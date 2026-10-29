"""Exceptions raised while parsing and running commands.

Each exception knows how to render itself for the user, which is what
:meth:`Command.main` relies on in standalone mode.
"""

from __future__ import annotations

import typing as t
from gettext import gettext as _
from gettext import ngettext

from ._compat import get_text_stderr
from .globals import resolve_color_default
from .utils import echo
from .utils import format_filename

if t.TYPE_CHECKING:
    from .core import Command
    from .core import Context
    from .core import Parameter


def _join_param_hints(hint: t.Sequence[str] | str | None) -> str | None:
    if hint is not None and not isinstance(hint, str):
        return " / ".join(repr(h) for h in hint)
    return hint


class ClickException(Exception):
    """Base for errors that are meant to be shown to the user. Exiting with
    one of these prints ``Error: <message>`` and uses :attr:`exit_code`.
    """

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # Remember the color setting of the active context, because the
        # exception may be shown after that context was torn down.
        self.show_color: bool | None = resolve_color_default()
        self.message = message

    def format_message(self) -> str:
        return self.message

    def __str__(self) -> str:
        return self.message

    def show(self, file: t.IO[t.Any] | None = None) -> None:
        if file is None:
            file = get_text_stderr()
        echo(
            _("Error: {message}").format(message=self.format_message()),
            file=file,
            color=self.show_color,
        )


class UsageError(ClickException):
    """The command line was used incorrectly. Shows the usage line and a
    hint about ``--help`` before the error message.
    """

    exit_code = 2

    def __init__(self, message: str, ctx: Context | None = None) -> None:
        super().__init__(message)
        self.ctx = ctx
        self.cmd: Command | None = ctx.command if ctx is not None else None

    def show(self, file: t.IO[t.Any] | None = None) -> None:
        if file is None:
            file = get_text_stderr()
        color = None
        hint = ""
        if self.ctx is not None:
            color = self.ctx.color
            if self.ctx.command.get_help_option(self.ctx) is not None:
                hint = _("Try '{command} {option}' for help.").format(
                    command=self.ctx.command_path,
                    option=self.ctx.help_option_names[0],
                )
                hint = f"{hint}\n"
            echo(f"{self.ctx.get_usage()}\n{hint}", file=file, color=color)
        echo(
            _("Error: {message}").format(message=self.format_message()),
            file=file,
            color=color,
        )


class BadParameter(UsageError):
    """A parameter received a value it cannot work with."""

    def __init__(
        self,
        message: str,
        ctx: Context | None = None,
        param: Parameter | None = None,
        param_hint: t.Sequence[str] | str | None = None,
    ) -> None:
        super().__init__(message, ctx)
        self.param = param
        self.param_hint = param_hint

    def _hint(self) -> str | None:
        if self.param_hint is not None:
            return _join_param_hints(self.param_hint)
        if self.param is not None:
            return self.param.get_error_hint(self.ctx)  # type: ignore[arg-type]
        return None

    def format_message(self) -> str:
        hint = self._hint()
        if hint is None:
            return _("Invalid value: {message}").format(message=self.message)
        return _("Invalid value for {param_hint}: {message}").format(
            param_hint=hint, message=self.message
        )


class MissingParameter(BadParameter):
    """A required parameter was not provided."""

    def __init__(
        self,
        message: str | None = None,
        ctx: Context | None = None,
        param: Parameter | None = None,
        param_hint: t.Sequence[str] | str | None = None,
        param_type: str | None = None,
    ) -> None:
        super().__init__(message or "", ctx, param, param_hint)
        self.param_type = param_type

    def format_message(self) -> str:
        hint = self._hint()
        hint_part = f" {hint}" if hint else ""

        kind = self.param_type
        if kind is None and self.param is not None:
            kind = self.param.param_type_name

        msg = self.message
        if self.param is not None:
            extra = self.param.type.get_missing_message(
                param=self.param, ctx=self.ctx
            )
            if extra:
                msg = f"{msg}. {extra}" if msg else extra
        msg_part = f" {msg}" if msg else ""

        if kind == "argument":
            label = _("Missing argument")
        elif kind == "option":
            label = _("Missing option")
        elif kind == "parameter":
            label = _("Missing parameter")
        else:
            label = _("Missing {param_type}").format(param_type=kind)

        return f"{label}{hint_part}.{msg_part}"

    def __str__(self) -> str:
        if not self.message:
            name = self.param.name if self.param is not None else None
            return _("Missing parameter: {param_name}").format(param_name=name)
        return self.message


class NoSuchOption(UsageError):
    """An option that does not exist was given."""

    def __init__(
        self,
        option_name: str,
        message: str | None = None,
        possibilities: t.Sequence[str] | None = None,
        ctx: Context | None = None,
    ) -> None:
        if message is None:
            message = _("No such option: {name}").format(name=option_name)
        super().__init__(message, ctx)
        self.option_name = option_name
        self.possibilities = possibilities

    def format_message(self) -> str:
        if not self.possibilities:
            return self.message
        choices = sorted(self.possibilities)
        suggestion = ngettext(
            "Did you mean {possibility}?",
            "(Possible options: {possibilities})",
            len(choices),
        ).format(possibility=choices[0], possibilities=", ".join(choices))
        return f"{self.message} {suggestion}"


class BadOptionUsage(UsageError):
    """An option was used the wrong way, for example with the wrong number
    of values.
    """

    def __init__(
        self, option_name: str, message: str, ctx: Context | None = None
    ) -> None:
        super().__init__(message, ctx)
        self.option_name = option_name


class BadArgumentUsage(UsageError):
    """An argument was used the wrong way."""


class NoArgsIsHelpError(UsageError):
    """Raised when a command that wants arguments was called without any;
    it shows the full help page instead of an error.
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx: Context
        super().__init__(ctx.get_help(), ctx=ctx)

    def show(self, file: t.IO[t.Any] | None = None) -> None:
        echo(self.format_message(), file=file, err=True, color=self.ctx.color)


class FileError(ClickException):
    """A file could not be opened."""

    def __init__(self, filename: str, hint: str | None = None) -> None:
        if hint is None:
            hint = _("unknown error")
        super().__init__(hint)
        self.ui_filename: str = format_filename(filename)
        self.filename = filename

    def format_message(self) -> str:
        return _("Could not open file {filename!r}: {message}").format(
            filename=self.ui_filename, message=self.message
        )


class Abort(RuntimeError):
    """Stop execution; standalone mode prints ``Aborted!``."""


class Exit(RuntimeError):
    """Stop execution with the given exit code, without printing anything."""

    __slots__ = ("exit_code",)

    def __init__(self, code: int = 0) -> None:
        self.exit_code: int = code
