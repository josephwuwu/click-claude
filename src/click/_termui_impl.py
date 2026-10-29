"""Implementations behind :mod:`click.termui` that need more machinery:
the progress bar, the pager, the editor, URL launching and raw key input.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import io
import os
import sys
import typing as t
from gettext import gettext as _
from io import StringIO
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


def _default_text_stdout() -> t.TextIO | None:
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
            file = _default_text_stdout()
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
        import time

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
            import math

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

        import time

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
        """Advance by ``n_steps`` and optionally set the current item.

        Redrawing waits until ``update_min_steps`` steps accumulated, but
        the steps that complete the bar are always applied.
        """
        if current_item is not None:
            self.current_item = current_item

        self._completed_intervals += n_steps
        reaches_end = (
            self.length is not None
            and self.pos + self._completed_intervals >= self.length
        )
        if self._completed_intervals >= self.update_min_steps or reaches_end:
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

        if self._completed_intervals:
            self.make_step(self._completed_intervals)
            self._completed_intervals = 0
        self.finish()
        self.render_progress()


class _PagerWriter:
    """The file object handed out by :func:`click.get_pager_file`.

    It writes text to the pager's input, which may be a text or a binary
    stream, and removes styling unless :attr:`color` is set. Closing it
    only flushes: the stream belongs to the pager strategy.
    """

    def __init__(self, stream: t.IO[t.Any], color: bool) -> None:
        self._stream = stream
        self.color = color
        self._binary = not isinstance(stream, io.TextIOBase) and not hasattr(
            stream, "encoding"
        )
        self._encoding = get_best_encoding(sys.stdout) if sys.stdout else "utf-8"

    def write(self, text: str) -> int:
        if not self.color:
            text = strip_ansi(text)
        if self._binary:
            self._stream.write(text.encode(self._encoding, "replace"))
        else:
            self._stream.write(text)
        return len(text)

    def writelines(self, lines: cabc.Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self.flush()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def __enter__(self) -> _PagerWriter:
        return self

    def __exit__(self, *exc_info: t.Any) -> None:
        self.close()


def _wait_ignoring_interrupts(proc: t.Any) -> None:
    """Wait for the pager. Ctrl-C belongs to the pager while it runs."""
    while True:
        try:
            proc.wait()
        except KeyboardInterrupt:
            continue
        return


def _is_less(cmd_path: str | os.PathLike[str]) -> bool:
    stem = os.path.splitext(os.path.basename(os.fspath(cmd_path)))[0]
    if WIN:
        # Windows file names are case-insensitive.
        return stem.casefold() == "less"
    return stem == "less"


def _less_shows_raw_colors(tokens: cabc.Iterable[str]) -> bool:
    """Whether ``less`` options request raw control characters, which is
    what makes it render colors.
    """
    for token in tokens:
        if token == "--":
            break
        if token.startswith("--"):
            if token[2:] in ("RAW-CONTROL-CHARS", "raw-control-chars"):
                return True
        elif token.startswith("-") and len(token) > 1:
            if "r" in token[1:] or "R" in token[1:]:
                return True
    return False


def _split_less_env(value: str) -> list[str]:
    import shlex

    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


@contextlib.contextmanager
def _pipepager(
    cmd_path: str | os.PathLike[str], cmd_params: cabc.Sequence[str]
) -> cabc.Iterator[tuple[t.IO[t.Any], bool | None]]:
    """Run the pager and yield its input stream, plus whether it shows
    colors (``True``) or there is no opinion (``None``).

    If the output fails with an error, the pager is stopped. On Ctrl-C it
    stays open so the user can read what arrived; the interrupt is raised
    once they close it.
    """
    import subprocess

    env = dict(os.environ)
    color: bool | None = None

    if _is_less(cmd_path):
        less_env = os.environ.get("LESS", "")
        if not less_env and not cmd_params:
            env["LESS"] = "-R"
            color = True
        elif _less_shows_raw_colors([*_split_less_env(less_env), *cmd_params]):
            color = True

    proc = subprocess.Popen(
        [os.fspath(cmd_path), *cmd_params], stdin=subprocess.PIPE, env=env
    )
    stdin = proc.stdin
    assert stdin is not None

    def close_input() -> None:
        try:
            stdin.close()
        except BrokenPipeError:
            pass

    try:
        yield stdin, color
    except BrokenPipeError:
        # The user quit the pager before all output was written.
        pass
    except KeyboardInterrupt:
        close_input()
        _wait_ignoring_interrupts(proc)
        raise
    except BaseException:
        proc.terminate()
        close_input()
        proc.wait()
        raise

    close_input()
    _wait_ignoring_interrupts(proc)


@contextlib.contextmanager
def _tempfilepager(
    cmd_path: str | os.PathLike[str], cmd_params: cabc.Sequence[str]
) -> cabc.Iterator[tuple[t.IO[t.Any], bool | None]]:
    """Collect the output in a temporary file and show it with the pager
    afterwards. Used where pipes to interactive pagers do not work.
    """
    import subprocess
    import tempfile

    encoding = get_best_encoding(sys.stdout) if sys.stdout else "utf-8"
    f = tempfile.NamedTemporaryFile(
        mode="w", encoding=encoding, errors="replace", delete=False
    )
    try:
        try:
            yield f, None
        finally:
            # Close before the file is shown or removed, also on errors:
            # Windows refuses both while it is open.
            f.close()
        subprocess.call([os.fspath(cmd_path), *cmd_params, f.name])
    finally:
        os.unlink(f.name)


@contextlib.contextmanager
def _nullpager(
    stream: t.IO[t.Any],
) -> cabc.Iterator[tuple[t.IO[t.Any], bool | None]]:
    """Write straight to ``stream``, which is borrowed and stays open."""
    try:
        yield stream, None
    finally:
        stream.flush()


def _pager_command() -> list[str] | None:
    """The pager command from ``PAGER``, split into arguments. ``None`` if
    it is not set; an empty list if it is set but blank.
    """
    value = os.environ.get("PAGER")
    if value is None:
        return None
    import shlex

    return shlex.split(value)


@contextlib.contextmanager
def _pager_contextmanager(
    color: bool | None = None,
) -> cabc.Iterator[tuple[t.IO[t.Any], bool | None]]:
    """Pick how to page and yield the stream to write to, plus the pager's
    opinion about colors.

    Output that is not interactive is written directly. Otherwise the
    ``PAGER`` command is used, or a platform default. If no pager can be
    found, output is written directly too.
    """
    import shutil

    stdout = _default_text_stdout()
    if stdout is None:
        stdout = StringIO()

    if not isatty(sys.stdin) or not isatty(stdout):
        with _nullpager(stdout) as rv:
            yield rv
        return

    strategy = _tempfilepager if WIN else _pipepager

    command = _pager_command()
    if command:
        path = shutil.which(command[0])
        if path is not None:
            with strategy(path, command[1:]) as rv:
                yield rv
            return
        # A pager was asked for but is missing: do not guess another one.
        with _nullpager(stdout) as rv:
            yield rv
        return

    if os.environ.get("TERM") not in ("dumb", "emacs"):
        candidates = ("more",) if WIN or sys.platform.startswith("os2") else ("less", "more")
        for name in candidates:
            path = shutil.which(name)
            if path is not None:
                with strategy(path, []) as rv:
                    yield rv
                return

    with _nullpager(stdout) as rv:
        yield rv


def pager(generator: cabc.Iterable[str], color: bool | None = None) -> None:
    """Show text chunks in a pager, flushing after each one so that slow
    producers show up incrementally.
    """
    from .termui import get_pager_file

    with get_pager_file(color=color) as out:
        for chunk in generator:
            out.write(chunk)
            out.flush()


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
        import shutil

        for candidate in ("sensible-editor", "vim", "nano"):
            if shutil.which(candidate) is not None:
                return candidate
        return "vi"

    def edit_files(self, filenames: cabc.Iterable[str | os.PathLike[str]]) -> None:
        """Run the editor on the files. The editor setting is split like a
        shell command line, so it may carry arguments and quoted paths;
        no shell is involved.
        """
        import subprocess

        editor = self.get_editor()
        env = None
        if self.env:
            env = dict(os.environ)
            env.update(self.env)

        import shlex

        args = [*shlex.split(editor), *(os.fspath(f) for f in filenames)]
        try:
            proc = subprocess.Popen(args, env=env)
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
    @contextlib.contextmanager
    def raw_terminal() -> cabc.Iterator[int]:
        import termios
        import tty

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
