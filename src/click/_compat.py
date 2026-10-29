"""Small helpers that smooth over platform and stream differences.

Everything in here is internal; the public API lives in the other modules.
"""

from __future__ import annotations

import codecs
import io
import os
import re
import sys
import typing as t

WIN = sys.platform.startswith("win")
CYGWIN = sys.platform.startswith("cygwin")

# A complete CSI escape sequence: ESC, "[", parameter bytes, intermediate
# bytes, and one final byte. This covers colors (including colon separated
# true color), cursor movement, private modes and mouse reports.
_ansi_re = re.compile(r"\033\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")


def strip_ansi(value: str) -> str:
    """Remove ANSI escape sequences from ``value``."""
    return _ansi_re.sub("", value)


def term_len(value: str) -> int:
    """Length of ``value`` as it would appear on a terminal."""
    return len(strip_ansi(value))


def _stream_is_tty(stream: t.Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def isatty(stream: t.Any) -> bool:
    return _stream_is_tty(stream)


def should_strip_ansi(stream: t.Any = None, color: bool | None = None) -> bool:
    """Decide whether styling should be removed before writing to
    ``stream``. An explicit ``color`` always wins; otherwise styling is
    kept for terminals and Jupyter notebooks.
    """
    if color is not None:
        return not color
    if stream is None:
        stream = sys.stdin
    return not isatty(stream) and not _is_jupyter_kernel_output(stream)


def _is_jupyter_kernel_output(stream: t.Any) -> bool:
    """Whether ``stream`` is the output of a Jupyter kernel, which renders
    styling although it is not a terminal.
    """
    module = type(stream).__module__ or ""
    return module.startswith("ipykernel.")


def is_ascii_encoding(encoding: str) -> bool:
    try:
        return codecs.lookup(encoding).name == "ascii"
    except LookupError:
        return False


def get_best_encoding(stream: t.Any) -> str:
    encoding = getattr(stream, "encoding", None) or sys.getdefaultencoding()
    if is_ascii_encoding(encoding):
        return "utf-8"
    return encoding


def _binary_of(stream: t.Any) -> t.BinaryIO | None:
    """Find the binary layer of a text stream, or the stream itself if it
    already is binary.
    """
    if stream is None:
        return None
    if isinstance(stream, (io.RawIOBase, io.BufferedIOBase)):
        return t.cast(t.BinaryIO, stream)
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        return t.cast(t.BinaryIO, buf)
    # Some file-likes accept bytes directly.
    try:
        stream.write(b"")
    except Exception:
        return None
    return t.cast(t.BinaryIO, stream)


def get_binary_stdin() -> t.BinaryIO:
    rv = _binary_of(sys.stdin)
    if rv is None:
        raise RuntimeError("Was not able to determine binary stream for sys.stdin.")
    return rv


def get_binary_stdout() -> t.BinaryIO:
    rv = _binary_of(sys.stdout)
    if rv is None:
        raise RuntimeError("Was not able to determine binary stream for sys.stdout.")
    return rv


def get_binary_stderr() -> t.BinaryIO:
    rv = _binary_of(sys.stderr)
    if rv is None:
        raise RuntimeError("Was not able to determine binary stream for sys.stderr.")
    return rv


def _wrap_text(
    binary: t.BinaryIO, encoding: str | None, errors: str | None
) -> t.TextIO:
    return io.TextIOWrapper(
        binary,  # type: ignore[arg-type]
        encoding=encoding or "utf-8",
        errors=errors or "replace",
        line_buffering=True,
    )


def _text_stream(
    stream: t.TextIO, encoding: str | None, errors: str | None
) -> t.TextIO:
    if encoding is None and errors is None:
        return stream
    if encoding is not None and getattr(stream, "encoding", None) == encoding:
        if errors is None or getattr(stream, "errors", None) == errors:
            return stream
    binary = _binary_of(stream)
    if binary is None:
        return stream
    return _wrap_text(binary, encoding, errors)


def get_text_stdin(encoding: str | None = None, errors: str | None = None) -> t.TextIO:
    return _text_stream(sys.stdin, encoding, errors)


def get_text_stdout(
    encoding: str | None = None, errors: str | None = None
) -> t.TextIO:
    return _text_stream(sys.stdout, encoding, errors)


def get_text_stderr(
    encoding: str | None = None, errors: str | None = None
) -> t.TextIO:
    return _text_stream(sys.stderr, encoding, errors)


binary_streams: dict[str, t.Callable[[], t.BinaryIO]] = {
    "stdin": get_binary_stdin,
    "stdout": get_binary_stdout,
    "stderr": get_binary_stderr,
}

text_streams: dict[str, t.Callable[[str | None, str | None], t.TextIO]] = {
    "stdin": get_text_stdin,
    "stdout": get_text_stdout,
    "stderr": get_text_stderr,
}


def get_filesystem_encoding() -> str:
    return sys.getfilesystemencoding() or sys.getdefaultencoding()


class _AtomicFile:
    """Writes go to a temporary file in the target directory that is renamed
    over the real filename once the file is closed without an error.
    """

    def __init__(self, f: t.IO[t.Any], tmp_filename: str, real_filename: str):
        self._f = f
        self._tmp_filename = tmp_filename
        self._real_filename = real_filename
        self.closed = False

    @property
    def name(self) -> str:
        return self._real_filename

    def close(self, delete: bool = False) -> None:
        if self.closed:
            return
        self._f.close()
        os.replace(self._tmp_filename, self._real_filename)
        self.closed = True

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._f, name)

    def __enter__(self) -> _AtomicFile:
        return self

    def __exit__(self, exc_type: t.Any, exc_value: t.Any, tb: t.Any) -> None:
        self.close(delete=exc_type is not None)

    def __repr__(self) -> str:
        return repr(self._f)


def open_stream(
    filename: str | os.PathLike[str],
    mode: str = "r",
    encoding: str | None = None,
    errors: str | None = "strict",
    atomic: bool = False,
) -> tuple[t.IO[t.Any], bool]:
    """Open ``filename`` and return ``(file, should_close)``. ``"-"`` maps to
    the standard streams, which must not be closed by the caller.
    """
    binary = "b" in mode
    filename = os.fspath(filename)

    if isinstance(filename, bytes):
        filename = filename.decode(get_filesystem_encoding())

    if filename == "-":
        if any(m in mode for m in "wax"):
            if binary:
                return get_binary_stdout(), False
            return get_text_stdout(encoding=encoding, errors=errors), False
        if binary:
            return get_binary_stdin(), False
        return get_text_stdin(encoding=encoding, errors=errors), False

    if not atomic:
        if binary:
            return open(filename, mode), True
        return open(filename, mode, encoding=encoding, errors=errors), True

    if "a" in mode:
        raise ValueError(
            "Appending to an existing file is not supported, because that"
            " would involve an expensive `copy`-operation to a temporary"
            " file. Open the file in normal `w`-mode and copy explicitly"
            " if that's what you're after."
        )
    if "x" in mode:
        raise ValueError("Use the `overwrite`-parameter instead.")
    if "w" not in mode:
        raise ValueError("Atomic writes only make sense with `w`-mode.")

    import random
    import stat

    try:
        perm: int | None = os.stat(filename).st_mode
    except OSError:
        perm = None

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if binary:
        flags |= getattr(os, "O_BINARY", 0)

    directory = os.path.dirname(filename) or "."
    base = os.path.basename(filename)
    while True:
        suffix = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
        tmp_filename = os.path.join(directory, f".__atomic-write{suffix}-{base}")
        try:
            fd = os.open(tmp_filename, flags, 0o666 if perm is None else perm)
            break
        except FileExistsError:
            continue

    if perm is not None:
        os.chmod(tmp_filename, stat.S_IMODE(perm))

    f = os.fdopen(fd, mode, encoding=None if binary else encoding, errors=None if binary else errors)
    return t.cast(t.IO[t.Any], _AtomicFile(f, tmp_filename, os.path.realpath(filename))), True
