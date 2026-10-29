"""Implementations behind :mod:`click.termui` that need more machinery:
the progress bar, the pager, the editor, URL launching and raw key input.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import math
import os
import shlex
import sys
import time
import typing as t
from gettext import gettext as _
from io import StringIO
from shutil import which
from types import TracebackType

from ._compat import CYGWIN
from ._compat import WIN
from ._compat import get_best_encoding
from ._compat import isatty
from ._compat import strip_ansi
from ._compat import term_len
from .exceptions import ClickException
from .utils import echo

V = t.TypeVar("V")

if WIN:
    BEFORE_BAR = "\r"
    AFTER_BAR = "\n"
else:
    # Hide the cursor while the bar is drawn and show it again after.
    BEFORE_BAR = "\r\033[?25l"
    AFTER_BAR = "\033[?25h\n"


def _default_stdout() -> t.TextIO | None:
    return sys.stdout


class ProgressBar(t.Generic[V]):
    """Created by :func:`click.progressbar`."""

    def __init__(
        self,
        iterable: cabc.Iterable[V] | None,
        length: int | None = None,
        fill_char: str = "#",
        empty_char: str = " ",
        bar_template: str = "%(bar)s",
        info_sep: str = "  ",
        hidden: bool = False,
        show_eta: bool = True,
        show_percent: bool | None = None,
        show_pos: bool = False,
        item_show_func: t.Callable[[V | None], str | None] | None = None,
        label: str | None = None,
        file: t.TextIO | None = None,
        color: bool | None = None,
        update_min_steps: int = 1,
        width: int = 30,
    ) -> None:
        self.fill_char = fill_char
        self.empty_char = empty_char
        self.bar_template = bar_template
        self.info_sep = info_sep
        self.hidden = hidden
        self.show_eta = show_eta
        self.show_percent = show_percent
        self.show_pos = show_pos
        self.item_show_func = item_show_func
        self.label: str = label or ""

        if file is None:
            file = _default_stdout()
            if file is None:
                # No console at all, e.g. pythonw.
                file = StringIO()
        self.file = file
        self.color = color
        self.update_min_steps = update_min_steps
        self._completed_intervals = 0
        self.width: int = width
        self.autowidth: bool = width == 0

        if length is None and iterable is not None:
            from operator import length_hint

            hint = length_hint(iterable, -1)
            length = None if hint == -1 else hint

        if iterable is None:
            if length is None:
                raise TypeError("iterable or length is required")
            iterable = t.cast("cabc.Iterable[V]", range(length))

        self.iter: cabc.Iterator[V] = iter(iterable)
        self.length = length
        self.pos = 0
        self.avg: list[float] = []
        self.last_eta: float
        self.start: float
        self.start = self.last_eta = time.time()
        self.eta_known: bool = False
        self.finished: bool = False
        self.max_width: int | None = None
        self.entered: bool = False
        self.current_item: V | None = None
        self._is_atty = isatty(self.file)
        self._last_line: str | None = None

    def __enter__(self) -> ProgressBar[V]:
        self.entered = True
        self.render_progress()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.render_finish()

    def __iter__(self) -> cabc.Iterator[V]:
        if not self.entered:
            raise RuntimeError("You need to use progress bars in a with block.")
        self.render_progress()
        return self.generator()

    def __next__(self) -> V:
        # Lets ``next(bar)`` work, though iterating is the intended use.
        return next(iter(self))

    def render_finish(self) -> None:
        if self.hidden or not self._is_atty:
            return
        self.file.write(AFTER_BAR)
        self.file.flush()

    @property
    def pct(self) -> float:
        if self.finished:
            return 1.0
        return min(self.pos / (float(self.length or 1) or 1), 1.0)

    @property
    def time_per_iteration(self) -> float:
        if not self.avg:
            return 0.0
        return sum(self.avg) / float(len(self.avg))

    @property
    def eta(self) -> float:
        if self.length is not None and not self.finished:
            return self.time_per_iteration * (self.length - self.pos)
        return 0.0

    def format_eta(self) -> str:
        if not self.eta_known:
            return ""
        remaining = int(self.eta)
        days, remaining = divmod(remaining, 86400)
        hours, remaining = divmod(remaining, 3600)
        minutes, seconds = divmod(remaining, 60)
        clock = f"{hours:02}:{minutes:02}:{seconds:02}"
        return f"{days}d {clock}" if days > 0 else clock

    def format_pos(self) -> str:
        if self.length is not None:
            return f"{self.pos}/{self.length}"
        return str(self.pos)

    def format_pct(self) -> str:
        return f"{int(self.pct * 100): 4}%"[1:]

    def format_bar(self) -> str:
        if self.length is not None:
            filled = int(self.pct * self.width)
            return self.fill_char * filled + self.empty_char * (self.width - filled)
        if self.finished:
            return self.fill_char * self.width
        # Unknown length: a marker that swings back and forth.
        cells = list(self.empty_char * (self.width or 1))
        if self.time_per_iteration != 0:
            spot = (math.cos(self.pos * self.time_per_iteration) / 2.0 + 0.5) * self.width
            cells[int(spot)] = self.fill_char
        return "".join(cells)

    def format_progress_line(self) -> str:
        show_percent = self.show_percent
        if show_percent is None and self.length is not None:
            show_percent = not self.show_pos

        info = []
        if self.show_pos:
            info.append(self.format_pos())
        if show_percent:
            info.append(self.format_pct())
        if self.show_eta and self.eta_known and not self.finished:
            info.append(self.format_eta())
        if self.item_show_func is not None:
            item_text = self.item_show_func(self.current_item)
            if item_text is not None:
                info.append(item_text)

        line = self.bar_template % {
            "label": self.label,
            "bar": self.format_bar(),
            "info": self.info_sep.join(info),
        }
        return line.rstrip()

    def render_progress(self) -> None:
        if self.hidden:
            return

        if not self._is_atty:
            # Not a terminal: print the label once and nothing else.
            if self._last_line != self.label:
                self._last_line = self.label
                echo(self.label, file=self.file, color=self.color)
            return

        parts = []
        if self.autowidth:
            import shutil

            previous = self.width
            self.width = 0
            clutter = term_len(self.format_progress_line())
            new_width = max(0, shutil.get_terminal_size().columns - clutter)
            if new_width < previous and self.max_width is not None:
                # The terminal shrank; blank out the old, longer line.
                parts.append(BEFORE_BAR)
                parts.append(" " * self.max_width)
                self.max_width = new_width
            self.width = new_width

        clear_width = self.width if self.max_width is None else self.max_width

        parts.append(BEFORE_BAR)
        line = self.format_progress_line()
        line_len = term_len(line)
        if self.max_width is None or self.max_width < line_len:
            self.max_width = line_len
        parts.append(line)
        parts.append(" " * (clear_width - line_len))
        rendered = "".join(parts)

        if rendered != self._last_line:
            self._last_line = rendered
            echo(rendered, file=self.file, color=self.color, nl=False)
            self.file.flush()

    def make_step(self, n_steps: int) -> None:
        self.pos += n_steps
        if self.length is not None and self.pos >= self.length:
            self.finished = True

        now = time.time()
        if now - self.last_eta < 1.0:
            return
        self.last_eta = now

        # Keep a short rolling window of seconds per item.
        elapsed = now - self.start
        step = elapsed / self.pos if self.pos else elapsed
        self.avg = self.avg[-6:] + [step]
        self.eta_known = self.length is not None

    def update(self, n_steps: int, current_item: V | None = None) -> None:
        """Advance by ``n_steps`` and optionally set the current item."""
        if current_item is not None:
            self.current_item = current_item

        self._completed_intervals += n_steps
        if self._completed_intervals >= self.update_min_steps:
            self.make_step(self._completed_intervals)
            self.render_progress()
            self._completed_intervals = 0

    def finish(self) -> None:
        self.eta_known = False
        self.current_item = None
        self.finished = True

    def generator(self) -> cabc.Iterator[V]:
        if not self.entered:
            raise RuntimeError("You need to use progress bars in a with block.")

        if not self._is_atty:
            yield from self.iter
            return

        for item in self.iter:
            self.current_item = item
            # Draw before handing out the item so the item text is current.
            if self._completed_intervals == 0:
                self.render_progress()
            yield item
            self.update(1)

        self.finish()
        self.render_progress()


def pager(generator: cabc.Iterable[str], color: bool | None = None) -> None:
    """Send the text through a pager, or print it directly if output is
    not interactive.
    """
    stdout = _default_stdout()
    if stdout is None:
        stdout = StringIO()

    if not isatty(sys.stdin) or not isatty(stdout):
        return _nullpager(stdout, generator, color)

    command = (os.environ.get("PAGER") or "").strip()
    if command:
        if WIN:
            if _tempfilepager(generator, command, color):
                return
        elif _pipepager(generator, command, color):
            return

    if os.environ.get("TERM") in ("dumb", "emacs"):
        return _nullpager(stdout, generator, color)
    if (WIN or sys.platform.startswith("os2")) and _tempfilepager(generator, "more", color):
        return
    if _pipepager(generator, "less", color):
        return
    if _pipepager(generator, "more", color):
        return
    return _nullpager(stdout, generator, color)


def _pipepager(generator: cabc.Iterable[str], command: str, color: bool | None) -> bool:
    """Pipe the text into ``command``. Returns ``False`` if the command is
    not available.
    """
    import subprocess

    parts = shlex.split(command)
    if not parts:
        return False
    executable = which(parts[0])
    if executable is None:
        return False

    env = dict(os.environ)
    name = os.path.basename(executable).lower()
    if name.endswith(".exe"):
        name = name[:-4]

    if name == "less":
        # less shows escape codes raw unless told otherwise; -R passes
        # colors through.
        flags = os.environ.get("LESS", "") + "".join(parts[1:])
        if not flags:
            env["LESS"] = "-R"
            color = True
        elif "r" in flags or "R" in flags:
            color = True

    proc = subprocess.Popen(
        [executable, *parts[1:]],
        shell=False,
        stdin=subprocess.PIPE,
        env=env,
        errors="replace",
        text=True,
    )
    assert proc.stdin is not None
    try:
        for chunk in generator:
            if not color:
                chunk = strip_ansi(chunk)
            proc.stdin.write(chunk)
    except BrokenPipeError:
        # The pager was closed early; that is fine.
        pass
    except Exception as e:
        proc.terminate()
        raise e
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass

    # Wait for the pager; ignore Ctrl-C so the pager can handle it.
    while True:
        try:
            proc.wait()
        except KeyboardInterrupt:
            continue
        break
    return True


def _tempfilepager(generator: cabc.Iterable[str], command: str, color: bool | None) -> bool:
    """Write the text to a file and run ``command`` on it."""
    import subprocess
    import tempfile

    parts = shlex.split(command)
    if not parts:
        return False
    executable = which(parts[0])
    if executable is None:
        return False

    text = "".join(generator)
    if not color:
        text = strip_ansi(text)
    encoding = get_best_encoding(sys.stdout)

    fd, filename = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode(encoding))
        subprocess.call([executable, *parts[1:], filename])
    finally:
        os.unlink(filename)
    return True


def _nullpager(stream: t.TextIO, generator: cabc.Iterable[str], color: bool | None) -> None:
    for chunk in generator:
        if not color:
            chunk = strip_ansi(chunk)
        stream.write(chunk)


class Editor:
    def __init__(
        self,
        editor: str | None = None,
        env: cabc.Mapping[str, str] | None = None,
        require_save: bool = True,
        extension: str = ".txt",
    ) -> None:
        self.editor = editor
        self.env = env
        self.require_save = require_save
        self.extension = extension

    def get_editor(self) -> str:
        if self.editor is not None:
            return self.editor
        for key in ("VISUAL", "EDITOR"):
            value = os.environ.get(key)
            if value:
                return value
        if WIN:
            return "notepad"
        for candidate in ("sensible-editor", "vim", "nano"):
            if which(candidate) is not None:
                return candidate
        return "vi"

    def edit_files(self, filenames: cabc.Iterable[str]) -> None:
        import subprocess

        editor = self.get_editor()
        env = None
        if self.env:
            env = dict(os.environ)
            env.update(self.env)

        quoted = " ".join(f'"{name}"' for name in filenames)
        try:
            proc = subprocess.Popen(f"{editor} {quoted}", env=env, shell=True)
            code = proc.wait()
        except OSError as e:
            raise ClickException(
                _("{editor}: Editing failed: {e}").format(editor=editor, e=e)
            ) from e
        if code != 0:
            raise ClickException(_("{editor}: Editing failed").format(editor=editor))

    def edit(self, text: str | bytes | bytearray | None) -> str | bytes | None:
        import tempfile

        if not text:
            data = b""
        elif isinstance(text, (bytes, bytearray)):
            data = bytes(text)
        else:
            if not text.endswith("\n"):
                text += "\n"
            if WIN:
                data = text.replace("\n", "\r\n").encode("utf-8-sig")
            else:
                data = text.encode("utf-8")

        fd, name = tempfile.mkstemp(prefix="editor-", suffix=self.extension)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)

            # Backdate the file so that a save within the filesystem's
            # timestamp resolution is still detected.
            os.utime(name, (os.path.getatime(name), os.path.getmtime(name) - 2))
            before = os.path.getmtime(name)

            self.edit_files((name,))

            if self.require_save and os.path.getmtime(name) == before:
                return None

            with open(name, "rb") as f:
                result = f.read()

            if isinstance(text, (bytes, bytearray)):
                return result
            return result.decode("utf-8-sig").replace("\r\n", "\n")
        finally:
            os.unlink(name)


def open_url(url: str, wait: bool = False, locate: bool = False) -> int:
    import subprocess

    def local_path(value: str) -> str:
        from urllib.parse import unquote

        if value.startswith("file://"):
            return unquote(value[7:])
        return value

    if sys.platform == "darwin":
        args = ["open"]
        if wait:
            args.append("-W")
        if locate:
            args.append("-R")
        args.append(local_path(url))
        with open(os.devnull, "w") as null:
            return subprocess.Popen(args, stderr=null).wait()

    if WIN:
        if locate:
            args = ["explorer", f"/select,{local_path(url)}"]
        else:
            args = ["start"]
            if wait:
                args.append("/WAIT")
            args.extend(["", url])
        try:
            return subprocess.call(args, shell=not locate)
        except OSError:
            return 127

    if CYGWIN:
        if locate:
            args = ["cygstart", os.path.dirname(local_path(url))]
        else:
            args = ["cygstart"]
            if wait:
                args.append("-w")
            args.append(url)
        try:
            return subprocess.call(args)
        except OSError:
            return 127

    try:
        target = (os.path.dirname(local_path(url)) or ".") if locate else local_path(url)
        proc = subprocess.Popen(["xdg-open", target])
        if wait:
            return proc.wait()
        return 0
    except OSError:
        if url.startswith(("http://", "https://")) and not locate and not wait:
            import webbrowser

            webbrowser.open(url)
            return 0
        return 1


def _translate_ch_to_exc(ch: str) -> None:
    if ch == "\x03":
        raise KeyboardInterrupt()
    if ch == "\x04" and not WIN:
        raise EOFError()
    if ch == "\x1a" and WIN:
        raise EOFError()
    return None


if sys.platform == "win32":
    import msvcrt

    @contextlib.contextmanager
    def raw_terminal() -> cabc.Iterator[int]:
        yield -1

    def getchar(echo: bool) -> str:
        reader = msvcrt.getwche if echo else msvcrt.getwch  # type: ignore[attr-defined]
        ch = reader()
        # Function and arrow keys arrive as a prefix plus a key code.
        if ch in ("\x00", "\xe0"):
            ch += reader()
        _translate_ch_to_exc(ch)
        return ch

else:
    import termios
    import tty

    @contextlib.contextmanager
    def raw_terminal() -> cabc.Iterator[int]:
        f: t.TextIO | None
        if isatty(sys.stdin):
            fd = sys.stdin.fileno()
            f = None
        else:
            f = open("/dev/tty")
            fd = f.fileno()
        try:
            saved = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                yield fd
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                sys.stdout.flush()
        except termios.error:
            pass
        finally:
            if f is not None:
                f.close()

    def getchar(echo: bool) -> str:
        with raw_terminal() as fd:
            ch = os.read(fd, 32).decode(get_best_encoding(sys.stdin), "replace")
            if echo and isatty(sys.stdout):
                sys.stdout.write(ch)
            _translate_ch_to_exc(ch)
            return ch
