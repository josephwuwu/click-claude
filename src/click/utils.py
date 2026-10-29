"""General purpose helpers: printing, file handling and a few odds and ends
used throughout the package.
"""

from __future__ import annotations

import collections.abc as cabc
import os
import re
import sys
import typing as t
from functools import update_wrapper
from types import ModuleType
from types import TracebackType

from ._compat import WIN
from ._compat import _binary_of
from ._compat import binary_streams
from ._compat import get_filesystem_encoding
from ._compat import open_stream
from ._compat import should_strip_ansi
from ._compat import strip_ansi
from ._compat import text_streams
from .globals import resolve_color_default

if t.TYPE_CHECKING:
    import typing_extensions as te

    P = te.ParamSpec("P")

R = t.TypeVar("R")


def _posixify(name: str) -> str:
    return "-".join(name.split()).lower()


def _safecall(func: t.Callable[P, R]) -> t.Callable[P, R | None]:
    """Wrap ``func`` so that any exception it raises is swallowed."""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return func(*args, **kwargs)
        except Exception:
            return None

    return update_wrapper(wrapper, func)


def make_str(value: t.Any) -> str:
    """Turn ``value`` into text, decoding bytes with the filesystem encoding
    and falling back to UTF-8.
    """
    if isinstance(value, bytes):
        try:
            return value.decode(get_filesystem_encoding())
        except UnicodeError:
            return value.decode("utf-8", "replace")
    return str(value)


def _make_default_short_help(help: str, max_length: int = 45) -> str:
    """Derive a one-line summary from a help text.

    Only the first paragraph is considered. The summary ends at the first
    sentence if that fits, otherwise it is cut at a word boundary and an
    ellipsis is appended so that the result is at most ``max_length``
    characters long.
    """
    para_end = help.find("\n\n")
    if para_end != -1:
        help = help[:para_end]

    words = help.split()
    if words and words[0] == "\b":
        words = words[1:]
    if not words:
        return ""

    # First see whether whole words up to the end of a sentence fit.
    length = 0
    for idx, word in enumerate(words):
        length += len(word) + (1 if idx else 0)
        if length > max_length:
            break
        if word.endswith("."):
            return " ".join(words[: idx + 1])
    else:
        return " ".join(words)

    # Too long: drop words from the end until the text plus "..." fits.
    budget = max_length - 3
    kept: list[str] = []
    length = 0
    for word in words:
        extra = len(word) + (1 if kept else 0)
        if length + extra > budget:
            break
        kept.append(word)
        length += extra
    return " ".join(kept) + "..."


class _LazyFile:
    """A file that is only opened on first use. Files opened for reading
    are checked right away so that errors show up early; files for writing
    are only created when something is written.
    """

    def __init__(
        self,
        filename: str | os.PathLike[str],
        mode: str = "r",
        encoding: str | None = None,
        errors: str | None = "strict",
        atomic: bool = False,
    ):
        self.name: str = os.fspath(filename)  # type: ignore[assignment]
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.atomic = atomic
        self._f: t.IO[t.Any] | None
        self.should_close: bool

        if self.name == "-":
            self._f, self.should_close = open_stream(filename, mode, encoding, errors)
        else:
            if "r" in mode:
                # Fail now rather than at some later point. The file is
                # opened again when it is actually used.
                open_stream(filename, mode, encoding, errors)[0].close()
            self._f = None
            self.should_close = True

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self.open(), name)

    def __repr__(self) -> str:
        if self._f is not None:
            return repr(self._f)
        return f"<unopened file '{format_filename(self.name)}' {self.mode}>"

    def open(self) -> t.IO[t.Any]:
        """Open the file if that has not happened yet and return it."""
        if self._f is not None:
            return self._f
        try:
            rv, self.should_close = open_stream(
                self.name, self.mode, self.encoding, self.errors, atomic=self.atomic
            )
        except OSError as e:
            from .exceptions import FileError

            raise FileError(self.name, hint=e.strerror) from e
        self._f = rv
        return rv

    def close(self) -> None:
        if self._f is not None:
            self._f.close()

    def close_intelligently(self) -> None:
        """Close the file unless it is one of the standard streams."""
        if self.should_close:
            self.close()

    def __enter__(self) -> _LazyFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close_intelligently()

    def __iter__(self) -> cabc.Iterator[t.AnyStr]:
        self.open()
        return iter(self._f)  # type: ignore[arg-type]


class _KeepOpenFile:
    """Proxy for a file that should survive being used in a ``with`` block,
    such as ``sys.stdout``.
    """

    def __init__(self, file: t.IO[t.Any]) -> None:
        self._file: t.IO[t.Any] = file

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._file, name)

    def __enter__(self) -> _KeepOpenFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def __repr__(self) -> str:
        return repr(self._file)

    def __iter__(self) -> cabc.Iterator[t.AnyStr]:
        return iter(self._file)


def _default_text_stdout() -> t.TextIO | None:
    """The stream :func:`echo` writes to by default. ``None`` when there
    is no console, for example under pythonw.
    """
    return sys.stdout


def _default_text_stderr() -> t.TextIO | None:
    return sys.stderr


def echo(
    message: t.Any | None = None,
    file: t.IO[t.Any] | None = None,
    nl: bool = True,
    err: bool = False,
    color: bool | None = None,
) -> None:
    """Print a message and a newline to stdout or another file.

    Unlike :func:`print`, this accepts bytes as well as text, removes ANSI
    styling when the target is not a terminal (unless ``color`` says
    otherwise) and always flushes.

    :param message: what to print. Objects that are not text or bytes are
        converted with :func:`str`.
    :param file: where to write. Defaults to stdout, or stderr if ``err``.
    :param nl: append a newline.
    :param err: write to stderr instead of stdout.
    :param color: force styling on or off.
    """
    if file is None:
        file = _default_text_stderr() if err else _default_text_stdout()
        if file is None:
            # No console, for example under pythonw.
            return

    out: str | bytes | bytearray | None
    if message is None or isinstance(message, (str, bytes, bytearray)):
        out = message
    else:
        out = str(message)

    if nl:
        if out is None:
            out = "\n"
        elif isinstance(out, str):
            out = out + "\n"
        else:
            out = bytes(out) + b"\n"

    if not out:
        file.flush()
        return

    if isinstance(out, (bytes, bytearray)):
        binary = _binary_of(file)
        if binary is not None and binary is not file:
            file.flush()
            binary.write(out)
            binary.flush()
            return
        if binary is None:
            out = bytes(out).decode(getattr(file, "encoding", None) or "utf-8", "replace")
        else:
            binary.write(out)
            binary.flush()
            return
    else:
        color = resolve_color_default(color)
        if should_strip_ansi(file, color):
            out = strip_ansi(out)

    file.write(out)  # type: ignore[arg-type]
    file.flush()


def _get_binary_stream(name: t.Literal["stdin", "stdout", "stderr"]) -> t.BinaryIO:
    """Return the binary layer of a standard stream."""
    opener = binary_streams.get(name)
    if opener is None:
        raise TypeError(f"Unknown standard stream '{name}'")
    return opener()


def _get_text_stream(
    name: t.Literal["stdin", "stdout", "stderr"],
    encoding: str | None = None,
    errors: str | None = "strict",
) -> t.TextIO:
    """Return a standard stream as text, optionally re-wrapped with a
    different encoding.
    """
    opener = text_streams.get(name)
    if opener is None:
        raise TypeError(f"Unknown standard stream '{name}'")
    return opener(encoding, errors)


def open_file(
    filename: str | os.PathLike[str],
    mode: str = "r",
    encoding: str | None = None,
    errors: str | None = "strict",
    lazy: bool = False,
    atomic: bool = False,
) -> t.IO[t.Any]:
    """Open a file, treating ``"-"`` as stdin or stdout. The standard
    streams are wrapped so that closing them in a ``with`` block does
    nothing.

    :param lazy: open the file only when it is first used.
    :param atomic: write to a temporary file and move it into place on
        close.
    """
    if lazy:
        return t.cast(
            "t.IO[t.Any]", _LazyFile(filename, mode, encoding, errors, atomic=atomic)
        )
    f, should_close = open_stream(filename, mode, encoding, errors, atomic=atomic)
    if not should_close:
        f = t.cast("t.IO[t.Any]", _KeepOpenFile(f))
    return f


def format_filename(
    filename: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    shorten: bool = False,
) -> str:
    """Make a filename safe to show to a user. Undecodable bytes are
    replaced instead of raising errors.

    :param shorten: only show the last path component.
    """
    if shorten:
        filename = os.path.basename(filename)
    else:
        filename = os.fspath(filename)

    if isinstance(filename, bytes):
        return filename.decode(get_filesystem_encoding(), "replace")
    return filename.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def get_app_dir(app_name: str, roaming: bool = True, force_posix: bool = False) -> str:
    """Return the directory where an application should keep its
    configuration, following the conventions of the operating system.

    :param roaming: on Windows, use the roaming profile.
    :param force_posix: use ``~/.app-name`` on all POSIX systems.
    """
    if WIN:
        key = "APPDATA" if roaming else "LOCALAPPDATA"
        base = os.environ.get(key) or os.path.expanduser("~")
        return os.path.join(base, app_name)
    if force_posix:
        return os.path.join(os.path.expanduser(f"~/.{_posixify(app_name)}"))
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"), app_name
        )
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, _posixify(app_name))


class _PacifyFlushWrapper:
    """Wrap a stream so that a broken pipe on flush is ignored. Used to
    keep the interpreter quiet when output is piped into a program that
    exits early.
    """

    def __init__(self, wrapped: t.IO[t.Any]) -> None:
        self.wrapped = wrapped

    def flush(self) -> None:
        try:
            self.wrapped.flush()
        except OSError as e:
            import errno

            if e.errno != errno.EPIPE:
                raise

    def __getattr__(self, attr: str) -> t.Any:
        return getattr(self.wrapped, attr)


def _detect_program_name(
    path: str | None = None, _main: ModuleType | None = None
) -> str:
    """Work out how the program was started so usage lines look right.
    For ``python -m pkg`` this returns ``"python -m pkg"``, otherwise the
    basename of the script.
    """
    if _main is None:
        _main = sys.modules["__main__"]
    if not path:
        path = sys.argv[0]

    package = getattr(_main, "__package__", None)
    if package is None:
        return os.path.basename(path)
    if (
        package == ""
        and WIN
        and not os.path.exists(path)
        and os.path.exists(f"{path}.exe")
    ):
        # A console-script entry point on Windows.
        return os.path.basename(path)

    module = package
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem != "__main__":
        module = f"{module}.{stem}"
    return f"python -m {module.lstrip('.')}"


def _expand_args(
    args: cabc.Iterable[str],
    *,
    user: bool = True,
    env: bool = True,
    glob_recursive: bool = True,
) -> list[str]:
    """Expand ``~``, environment variables and glob patterns in ``args``,
    which is what a POSIX shell does but the Windows shell does not.
    """
    from glob import glob

    out: list[str] = []
    for arg in args:
        if user:
            arg = os.path.expanduser(arg)
        if env:
            arg = os.path.expandvars(arg)
        try:
            matches = glob(arg, recursive=glob_recursive)
        except re.error:
            matches = []
        out.extend(matches if matches else [arg])
    return out


_DEPRECATED = {
    "LazyFile": "_LazyFile",
    "KeepOpenFile": "_KeepOpenFile",
    "make_default_short_help": "_make_default_short_help",
    "PacifyFlushWrapper": "_PacifyFlushWrapper",
    "safecall": "_safecall",
    "get_binary_stream": "_get_binary_stream",
    "get_text_stream": "_get_text_stream",
}


def __getattr__(name: str) -> object:
    if name in _DEPRECATED:
        import warnings

        warnings.warn(
            f"'utils.{name}' is deprecated and will be removed in Click 9.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[_DEPRECATED[name]]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
