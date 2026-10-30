"""Helpers for testing command line applications: run a command in-process
with fake standard streams, environment and terminal input, and inspect
what happened.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import io
import os
import shlex
import sys
import tempfile
import typing as t
from types import TracebackType

from . import _compat
from . import formatting
from . import termui
from . import utils
from ._compat import _binary_of

if t.TYPE_CHECKING:
    from _typeshed import ReadableBuffer

    from .core import Command


#: How :class:`CliRunner` captures output.
CaptureMode = t.Literal["sys", "fd"]

#: The ``(type, value, traceback)`` triple of a caught exception.
ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType]


class EchoingStdin:
    """Stdin replacement that copies everything read to an output stream,
    so that input shows up in the captured output like on a terminal.
    """

    def __init__(self, input: t.BinaryIO, output: t.BinaryIO) -> None:
        self._input = input
        self._output = output
        self._paused = False

    def __getattr__(self, x: str) -> t.Any:
        return getattr(self._input, x)

    def _echo(self, rv: bytes) -> bytes:
        if not self._paused:
            self._output.write(rv)
        return rv

    def read(self, n: int = -1) -> bytes:
        return self._echo(self._input.read(n))

    def read1(self, n: int = -1) -> bytes:
        return self._echo(self._input.read1(n))  # type: ignore

    def readline(self, n: int = -1) -> bytes:
        return self._echo(self._input.readline(n))

    def readlines(self) -> list[bytes]:
        return [self._echo(line) for line in self._input.readlines()]

    def __iter__(self) -> cabc.Iterator[bytes]:
        return iter(self._echo(line) for line in self._input)

    def __repr__(self) -> str:
        return repr(self._input)


@contextlib.contextmanager
def _pause_echo(stream: EchoingStdin | None) -> cabc.Iterator[None]:
    if stream is None:
        yield
        return
    stream._paused = True
    try:
        yield
    finally:
        stream._paused = False


class BytesIOCopy(io.BytesIO):
    """A buffer that also copies everything written to a second buffer."""

    def __init__(self, copy_to: io.BytesIO) -> None:
        super().__init__()
        self.copy_to = copy_to

    def flush(self) -> None:
        super().flush()
        self.copy_to.flush()

    def write(self, b: ReadableBuffer) -> int:
        self.copy_to.write(b)
        return super().write(b)


class StreamMixer:
    """Separate stdout and stderr buffers plus one buffer with both,
    interleaved in the order they were written.
    """

    def __init__(self) -> None:
        self.output: io.BytesIO = io.BytesIO()
        self.stdout: io.BytesIO = BytesIOCopy(copy_to=self.output)
        self.stderr: io.BytesIO = BytesIOCopy(copy_to=self.output)


class _NamedTextIOWrapper(io.TextIOWrapper):
    def __init__(
        self,
        buffer: t.BinaryIO,
        name: str,
        mode: str,
        fileno: int | None = None,
        **kwargs: t.Any,
    ) -> None:
        super().__init__(buffer, **kwargs)
        self._name = name
        self._mode = mode
        self._fileno = fileno
        self._released = False

    def close(self) -> None:
        """Flush and let go of the buffer without closing it: the runner
        still reads the buffer after the code under test is done with the
        stream, even if it was closed or collected.
        """
        if getattr(self, "_released", True):
            return
        self._released = True
        with contextlib.suppress(Exception):
            self.flush()
        with contextlib.suppress(Exception):
            self.detach()

    def fileno(self) -> int:
        # With fd capture the descriptor is redirected too, so code that
        # needs a real descriptor (like faulthandler) can have it.
        if self._fileno is not None:
            return self._fileno
        return super().fileno()

    @property
    def name(self) -> str:
        return self._name

    @property
    def mode(self) -> str:
        return self._mode


def make_input_stream(
    input: str | bytes | t.IO[t.Any] | None, charset: str
) -> t.BinaryIO:
    """Turn the ``input`` given to the runner into a binary stream."""
    if hasattr(input, "read"):
        binary = _binary_of(input)
        if binary is not None:
            return binary
        raise TypeError("Could not find binary reader for input stream.")

    if input is None:
        data = b""
    elif isinstance(input, str):
        data = input.encode(charset)
    else:
        data = t.cast(bytes, input)
    return io.BytesIO(data)


class Result:
    """The outcome of :meth:`CliRunner.invoke`."""

    def __init__(
        self,
        runner: CliRunner,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
        output_bytes: bytes,
        return_value: t.Any,
        exit_code: int,
        exception: BaseException | None,
        exc_info: tuple[type[BaseException], BaseException, TracebackType]
        | None = None,
    ):
        self.runner = runner
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes
        self.output_bytes = output_bytes
        self.return_value = return_value
        self.exit_code = exit_code
        self.exception = exception
        self.exc_info = exc_info

    def _decode(self, data: bytes) -> str:
        return data.decode(self.runner.charset, "replace").replace("\r\n", "\n")

    @property
    def output(self) -> str:
        """Everything written to stdout and stderr, as the user would see
        it in a terminal.
        """
        return self._decode(self.output_bytes)

    @property
    def stdout(self) -> str:
        return self._decode(self.stdout_bytes)

    @property
    def stderr(self) -> str:
        return self._decode(self.stderr_bytes)

    def __repr__(self) -> str:
        status = repr(self.exception) if self.exception else "okay"
        return f"<{type(self).__name__} {status}>"


class _FdCapture:
    """Redirects the process's file descriptors 1 and 2 into temporary
    files, which catches output that bypasses ``sys.stdout`` and
    ``sys.stderr``: writes to the descriptors, stale stream references,
    subprocesses and C extensions.
    """

    def __init__(self) -> None:
        self._saved: dict[int, int] = {}
        self._files: dict[int, t.BinaryIO] = {}

    def start(self) -> None:
        for fd in (1, 2):
            # Flush what is pending for the real descriptor first.
            real = sys.__stdout__ if fd == 1 else sys.__stderr__
            if real is not None:
                with contextlib.suppress(Exception):
                    real.flush()
            self._files[fd] = t.cast(t.BinaryIO, tempfile.TemporaryFile())
            self._saved[fd] = os.dup(fd)
            os.dup2(self._files[fd].fileno(), fd)

    def stop(self) -> tuple[bytes, bytes]:
        captured = []
        for fd in (1, 2):
            saved = self._saved.get(fd)
            if saved is not None:
                os.dup2(saved, fd)
                os.close(saved)
            f = self._files.pop(fd)
            f.seek(0)
            captured.append(f.read())
            f.close()
        self._saved.clear()
        return captured[0], captured[1]


@contextlib.contextmanager
def _real_streams_for_pdb() -> cabc.Iterator[None]:
    """Make ``pdb.Pdb()`` talk to the terminal rather than the captured
    streams, so that a breakpoint in a command under test is usable.
    """
    import pdb

    original = pdb.Pdb.__init__

    def init(self: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
        # Positional order: completekey, stdin, stdout.
        if len(args) < 2:
            kwargs.setdefault("stdin", sys.__stdin__)
        if len(args) < 3:
            kwargs.setdefault("stdout", sys.__stdout__)
        original(self, *args, **kwargs)

    pdb.Pdb.__init__ = init  # type: ignore[method-assign]
    try:
        yield
    finally:
        pdb.Pdb.__init__ = original  # type: ignore[method-assign]


class CliRunner:
    """Invoke commands in isolation for tests.

    :param charset: encoding of the fake streams.
    :param env: environment overrides applied to every invocation.
    :param echo_stdin: copy input to the output, as a terminal would show
        typed text even where no prompt echoes it.
    :param catch_exceptions: catch exceptions other than ``SystemExit``
        and store them on the result.
    :param capture: ``"sys"`` replaces ``sys.stdout`` and ``sys.stderr``.
        ``"fd"`` also redirects the file descriptors 1 and 2, capturing
        output written around the Python streams. Not available on
        Windows.
    """

    def __init__(
        self,
        charset: str = "utf-8",
        env: cabc.Mapping[str, str | None] | None = None,
        echo_stdin: bool = False,
        catch_exceptions: bool = True,
        capture: t.Literal["sys", "fd"] = "sys",
    ) -> None:
        if capture not in ("sys", "fd"):
            raise ValueError(f"Invalid capture mode {capture!r}, use 'sys' or 'fd'.")
        if capture == "fd" and sys.platform == "win32":
            raise ValueError("capture='fd' is not supported on Windows.")
        self.charset = charset
        self.env: cabc.Mapping[str, str | None] = env or {}
        self.echo_stdin = echo_stdin
        self.catch_exceptions = catch_exceptions
        self.capture = capture

    def get_default_prog_name(self, cli: Command) -> str:
        return cli.name or "root"

    def make_env(
        self, overrides: cabc.Mapping[str, str | None] | None = None
    ) -> cabc.Mapping[str, str | None]:
        rv = dict(self.env)
        if overrides:
            rv.update(overrides)
        return rv

    @contextlib.contextmanager
    def isolation(
        self,
        input: str | bytes | t.IO[t.Any] | None = None,
        env: cabc.Mapping[str, str | None] | None = None,
        color: bool = False,
    ) -> cabc.Iterator[tuple[io.BytesIO, io.BytesIO, io.BytesIO]]:
        """Replace the standard streams, environment and terminal functions
        for the duration of the block. Yields the stdout, stderr and
        combined output buffers.

        :param input: data for stdin.
        :param env: environment variables to set; ``None`` values unset.
        :param color: keep ANSI styling in the output.
        """
        bytes_input = make_input_stream(input, self.charset)
        echo_input: EchoingStdin | None = None

        saved_streams = (sys.stdin, sys.stdout, sys.stderr)
        saved_width = formatting.FORCED_WIDTH
        formatting.FORCED_WIDTH = 80

        env = self.make_env(env)
        mixer = StreamMixer()

        if self.echo_stdin:
            bytes_input = echo_input = t.cast(
                t.BinaryIO, EchoingStdin(bytes_input, mixer.stdout)
            )

        sys.stdin = text_input = _NamedTextIOWrapper(
            bytes_input, encoding=self.charset, name="<stdin>", mode="r"
        )
        if self.echo_stdin:
            # Echo input as soon as it is read.
            text_input._CHUNK_SIZE = 1  # type: ignore

        fd_mode = self.capture == "fd"
        sys.stdout = out_wrapper = _NamedTextIOWrapper(
            mixer.stdout,
            encoding=self.charset,
            name="<stdout>",
            mode="w",
            fileno=1 if fd_mode else None,
        )
        sys.stderr = err_wrapper = _NamedTextIOWrapper(
            mixer.stderr,
            encoding=self.charset,
            name="<stderr>",
            mode="w",
            errors="backslashreplace",
            fileno=2 if fd_mode else None,
        )

        def read_line() -> str:
            line = text_input.readline()
            if not line:
                raise EOFError()
            return line.rstrip("\r\n")

        def visible_input(prompt: str | None = None) -> str:
            sys.stdout.write(prompt or "")
            with _pause_echo(echo_input):
                value = read_line()
            sys.stdout.write(f"{value}\n")
            sys.stdout.flush()
            return value

        def hidden_input(prompt: str | None = None) -> str:
            sys.stdout.write(f"{prompt or ''}\n")
            sys.stdout.flush()
            with _pause_echo(echo_input):
                return read_line()

        def read_char(echo: bool) -> str:
            with _pause_echo(echo_input):
                char = sys.stdin.read(1)
            if echo:
                sys.stdout.write(char)
            sys.stdout.flush()
            return char

        def strip_unless_color(
            stream: t.IO[t.Any] | None = None, color: bool | None = None
        ) -> bool:
            if color is None:
                return not default_color
            return not color

        default_color = color

        saved_visible = termui.visible_prompt_func
        saved_hidden = termui.hidden_prompt_func
        saved_getchar = termui._getchar
        saved_strip_utils = utils.should_strip_ansi
        saved_strip_compat = _compat.should_strip_ansi
        termui.visible_prompt_func = visible_input
        termui.hidden_prompt_func = hidden_input
        termui._getchar = read_char
        utils.should_strip_ansi = strip_unless_color  # type: ignore
        _compat.should_strip_ansi = strip_unless_color

        saved_env = {}
        fd_capture = _FdCapture() if fd_mode else None
        try:
            for key, value in env.items():
                saved_env[key] = os.environ.get(key)
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if fd_capture is not None:
                fd_capture.start()
            with _real_streams_for_pdb():
                yield (mixer.stdout, mixer.stderr, mixer.output)
        finally:
            if fd_capture is not None:
                with contextlib.suppress(Exception):
                    sys.stdout.flush()
                    sys.stderr.flush()
                fd_out, fd_err = fd_capture.stop()
                # Descriptor output cannot be interleaved exactly with the
                # Python level output, so it is appended.
                mixer.stdout.write(fd_out)
                mixer.stderr.write(fd_err)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            sys.stdin, sys.stdout, sys.stderr = saved_streams
            out_wrapper.close()
            err_wrapper.close()
            termui.visible_prompt_func = saved_visible
            termui.hidden_prompt_func = saved_hidden
            termui._getchar = saved_getchar
            utils.should_strip_ansi = saved_strip_utils
            _compat.should_strip_ansi = saved_strip_compat
            formatting.FORCED_WIDTH = saved_width

    def invoke(
        self,
        cli: Command,
        args: str | cabc.Sequence[str] | None = None,
        input: str | bytes | t.IO[t.Any] | None = None,
        env: cabc.Mapping[str, str | None] | None = None,
        catch_exceptions: bool | None = None,
        color: bool = False,
        mix_stderr: bool | None = None,
        **extra: t.Any,
    ) -> Result:
        """Run ``cli`` and return a :class:`Result`.

        :param args: arguments as a list, or a string split like a shell.
        :param input: data for stdin.
        :param env: environment overrides.
        :param catch_exceptions: overrides the runner's setting.
        :param color: keep ANSI styling in the output.
        :param mix_stderr: ignored; stdout and stderr are always available
            both separately and combined.
        :param extra: passed to :meth:`Command.main`.
        """
        if catch_exceptions is None:
            catch_exceptions = self.catch_exceptions

        exc_info = None
        with self.isolation(input=input, env=env, color=color) as streams:
            return_value = None
            exception: BaseException | None = None
            exit_code = 0

            if isinstance(args, str):
                args = shlex.split(args)

            prog_name = extra.pop("prog_name", None) or self.get_default_prog_name(cli)

            try:
                return_value = cli.main(args=args or (), prog_name=prog_name, **extra)
            except SystemExit as e:
                exc_info = sys.exc_info()
                code = e.code
                if code is None:
                    code = 0
                if code != 0:
                    exception = e
                if not isinstance(code, int):
                    sys.stdout.write(str(code))
                    sys.stdout.write("\n")
                    code = 1
                exit_code = code
            except Exception as e:
                if not catch_exceptions:
                    raise
                exception = e
                exit_code = 1
                exc_info = sys.exc_info()
            finally:
                sys.stdout.flush()
                sys.stderr.flush()

        # Read after the isolation ended, which adds descriptor output.
        stdout = streams[0].getvalue()
        stderr = streams[1].getvalue()
        output = streams[2].getvalue()

        return Result(
            runner=self,
            stdout_bytes=stdout,
            stderr_bytes=stderr,
            output_bytes=output,
            return_value=return_value,
            exit_code=exit_code,
            exception=exception,
            exc_info=exc_info,  # type: ignore
        )

    @contextlib.contextmanager
    def isolated_filesystem(
        self, temp_dir: str | os.PathLike[str] | None = None
    ) -> cabc.Iterator[str]:
        """Change into a new temporary directory for the duration of the
        block. It is deleted afterwards unless ``temp_dir`` was given.

        Deprecated: changing the working directory affects the whole
        process and is not thread safe. Create a temporary directory and
        pass absolute paths instead.
        """
        import warnings

        warnings.warn(
            "'isolated_filesystem' is deprecated and will be removed in Click"
            " 9.0. Create a temporary directory, for example with pytest's"
            " 'tmp_path', and pass absolute paths instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        cwd = os.getcwd()
        path = tempfile.mkdtemp(dir=temp_dir)
        os.chdir(path)
        try:
            yield path
        finally:
            os.chdir(cwd)
            if temp_dir is None:
                import shutil

                with contextlib.suppress(OSError):
                    shutil.rmtree(path)
