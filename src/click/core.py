"""The central objects: contexts, commands, groups and parameters."""

from __future__ import annotations

import collections.abc as cabc
import enum
import errno
import inspect
import os
import sys
import typing as t
from collections import Counter
from contextlib import AbstractContextManager
from contextlib import ExitStack
from contextlib import contextmanager
from functools import update_wrapper
from gettext import gettext as _
from gettext import ngettext
from itertools import repeat
from types import TracebackType

from . import types
from .exceptions import Abort
from .exceptions import BadParameter
from .exceptions import ClickException
from .exceptions import Exit
from .exceptions import MissingParameter
from .exceptions import NoArgsIsHelpError
from .exceptions import UsageError
from .formatting import HelpFormatter
from .formatting import join_options
from .globals import pop_context
from .globals import push_context
from .parser import _flag_needs_value
from .parser import _OptionParser
from .parser import _split_opt
from .termui import confirm
from .termui import prompt
from .termui import style
from .utils import PacifyFlushWrapper
from .utils import _detect_program_name
from .utils import _expand_args
from .utils import echo
from .utils import make_default_short_help
from .utils import make_str

if t.TYPE_CHECKING:
    from .shell_completion import CompletionItem

F = t.TypeVar("F", bound="t.Callable[..., t.Any]")
V = t.TypeVar("V")


def _complete_visible_commands(
    ctx: Context, incomplete: str
) -> cabc.Iterator[tuple[str, Command]]:
    """Commands of the context's group whose name starts with
    ``incomplete``, leaving out hidden ones.
    """
    group = t.cast(Group, ctx.command)
    for name in group.list_commands(ctx):
        if not name.startswith(incomplete):
            continue
        command = group.get_command(ctx, name)
        if command is not None and not command.hidden:
            yield name, command


def _check_nested_chain(
    base_command: Group, cmd_name: str, cmd: Command, register: bool = False
) -> None:
    if not base_command.chain or not isinstance(cmd, Group):
        return
    if register:
        message = (
            f"It is not possible to add the group {cmd_name!r} to another"
            f" group {base_command.name!r} that is in chain mode."
        )
    else:
        message = (
            f"Found the group {cmd_name!r} as subcommand to another group "
            f" {base_command.name!r} that is in chain mode. This is not supported."
        )
    raise RuntimeError(message)


def batch(iterable: cabc.Iterable[V], batch_size: int) -> list[tuple[V, ...]]:
    """Group ``iterable`` into tuples of ``batch_size`` items."""
    return list(zip(*repeat(iter(iterable), batch_size)))


@contextmanager
def augment_usage_errors(
    ctx: Context, param: Parameter | None = None
) -> cabc.Iterator[None]:
    """Fill in the context and parameter of usage errors raised inside the
    block, if the code raising them did not know them.
    """
    try:
        yield
    except BadParameter as e:
        if e.ctx is None:
            e.ctx = ctx
        if param is not None and e.param is None:
            e.param = param
        raise
    except UsageError as e:
        if e.ctx is None:
            e.ctx = ctx
        raise


def iter_params_for_processing(
    invocation_order: cabc.Sequence[Parameter],
    declaration_order: cabc.Sequence[Parameter],
) -> list[Parameter]:
    """Order parameters for processing: eager ones first, then in the order
    they appeared on the command line, then the rest as declared.
    """
    position = {}
    for idx, param in enumerate(invocation_order):
        position.setdefault(id(param), idx)

    def key(param: Parameter) -> tuple[bool, float]:
        return (not param.is_eager, position.get(id(param), float("inf")))

    return sorted(declaration_order, key=key)


class ParameterSource(enum.Enum):
    """Where the value of a parameter came from."""

    #: Given on the command line.
    COMMANDLINE = enum.auto()
    #: Read from an environment variable.
    ENVIRONMENT = enum.auto()
    #: The parameter's default.
    DEFAULT = enum.auto()
    #: The context's ``default_map``.
    DEFAULT_MAP = enum.auto()
    #: Entered at an interactive prompt.
    PROMPT = enum.auto()


def _deprecated_note(deprecated: bool | str) -> str:
    if isinstance(deprecated, str):
        return f"(DEPRECATED: {deprecated})"
    return "(DEPRECATED)"


class Context:
    """State for one level of command execution.

    A context is created for each command that runs and links to the
    context of the group that invoked it. It holds the parsed parameter
    values, the user object, and settings that subcommands inherit.

    :param command: the command this context belongs to.
    :param parent: the context of the invoking group.
    :param info_name: the name used to invoke the command, shown in usage.
    :param obj: arbitrary user data, inherited from the parent if not set.
    :param auto_envvar_prefix: prefix for automatic environment variables.
        Subcommands extend it with their own name.
    :param default_map: defaults for parameters, by parameter name. Nested
        dicts provide defaults for subcommands.
    :param terminal_width: width used for help output.
    :param max_content_width: maximum width used for help output.
    :param resilient_parsing: ignore errors and skip callbacks and
        prompts, used for shell completion.
    :param allow_extra_args: keep unknown positional arguments in
        :attr:`args` instead of failing.
    :param allow_interspersed_args: allow options after arguments.
    :param ignore_unknown_options: keep unknown options as arguments.
    :param help_option_names: option names for the help option.
    :param token_normalize_func: normalizes option and command names.
    :param color: force styled output on or off.
    :param show_default: show defaults in help for all options.
    """

    #: Used by :meth:`make_formatter`.
    formatter_class: type[HelpFormatter] = HelpFormatter

    def __init__(
        self,
        command: Command,
        parent: Context | None = None,
        info_name: str | None = None,
        obj: t.Any | None = None,
        auto_envvar_prefix: str | None = None,
        default_map: cabc.MutableMapping[str, t.Any] | None = None,
        terminal_width: int | None = None,
        max_content_width: int | None = None,
        resilient_parsing: bool = False,
        allow_extra_args: bool | None = None,
        allow_interspersed_args: bool | None = None,
        ignore_unknown_options: bool | None = None,
        help_option_names: list[str] | None = None,
        token_normalize_func: t.Callable[[str], str] | None = None,
        color: bool | None = None,
        show_default: bool | None = None,
    ) -> None:
        self.parent = parent
        self.command = command
        self.info_name = info_name
        self.params: dict[str, t.Any] = {}
        self.args: list[str] = []
        self._protected_args: list[str] = []
        self._opt_prefixes: set[str] = set(parent._opt_prefixes) if parent else set()

        def inherit(value: t.Any, attr: str) -> t.Any:
            if value is None and parent is not None:
                return getattr(parent, attr)
            return value

        if obj is None and parent is not None:
            obj = parent.obj
        self.obj: t.Any = obj
        self._meta: dict[str, t.Any] = getattr(parent, "meta", {})

        if (
            default_map is None
            and info_name is not None
            and parent is not None
            and parent.default_map is not None
        ):
            default_map = parent.default_map.get(info_name)
        self.default_map = default_map

        #: Name of the subcommand about to run, set by groups. ``"*"`` for
        #: chained groups when any subcommand runs.
        self.invoked_subcommand: str | None = None

        self.terminal_width: int | None = inherit(terminal_width, "terminal_width")
        self.max_content_width: int | None = inherit(
            max_content_width, "max_content_width"
        )

        if allow_extra_args is None:
            allow_extra_args = command.allow_extra_args
        self.allow_extra_args = allow_extra_args
        if allow_interspersed_args is None:
            allow_interspersed_args = command.allow_interspersed_args
        self.allow_interspersed_args: bool = allow_interspersed_args
        if ignore_unknown_options is None:
            ignore_unknown_options = command.ignore_unknown_options
        self.ignore_unknown_options: bool = ignore_unknown_options

        if help_option_names is None:
            help_option_names = parent.help_option_names if parent else ["--help"]
        self.help_option_names: list[str] = help_option_names

        self.token_normalize_func: t.Callable[[str], str] | None = inherit(
            token_normalize_func, "token_normalize_func"
        )
        self.resilient_parsing: bool = resilient_parsing

        if auto_envvar_prefix is None:
            if (
                parent is not None
                and parent.auto_envvar_prefix is not None
                and info_name is not None
            ):
                auto_envvar_prefix = f"{parent.auto_envvar_prefix}_{info_name.upper()}"
        else:
            auto_envvar_prefix = auto_envvar_prefix.upper()
        if auto_envvar_prefix is not None:
            auto_envvar_prefix = auto_envvar_prefix.replace("-", "_")
        self.auto_envvar_prefix: str | None = auto_envvar_prefix

        self.color: bool | None = inherit(color, "color")
        self.show_default: bool | None = inherit(show_default, "show_default")

        self._close_callbacks: list[t.Callable[[], t.Any]] = []
        self._depth = 0
        self._parameter_source: dict[str, ParameterSource] = {}
        self._exit_stack = ExitStack()

    @property
    def protected_args(self) -> list[str]:
        import warnings

        warnings.warn(
            "'protected_args' is deprecated and will be removed in Click 9.0."
            " 'args' will contain remaining unparsed tokens.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._protected_args

    def to_info_dict(self) -> dict[str, t.Any]:
        """Describe the context and its command for documentation tools."""
        return {
            "command": self.command.to_info_dict(self),
            "info_name": self.info_name,
            "allow_extra_args": self.allow_extra_args,
            "allow_interspersed_args": self.allow_interspersed_args,
            "ignore_unknown_options": self.ignore_unknown_options,
            "auto_envvar_prefix": self.auto_envvar_prefix,
        }

    def __enter__(self) -> Context:
        self._depth += 1
        push_context(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                self.close()
        finally:
            pop_context()

    @contextmanager
    def scope(self, cleanup: bool = True) -> cabc.Iterator[Context]:
        """Make this the current context inside the block. With
        ``cleanup=False`` the context is not closed when the block ends,
        even if this was the outermost use.
        """
        if not cleanup:
            self._depth += 1
        try:
            with self as rv:
                yield rv
        finally:
            if not cleanup:
                self._depth -= 1

    @property
    def meta(self) -> dict[str, t.Any]:
        """A dict shared by this context and all contexts nested in it,
        for extensions to store state.
        """
        return self._meta

    def make_formatter(self) -> HelpFormatter:
        return self.formatter_class(
            width=self.terminal_width, max_width=self.max_content_width
        )

    def with_resource(self, context_manager: AbstractContextManager[V]) -> V:
        """Enter ``context_manager`` and exit it when this context closes.
        Returns what entering it returned.
        """
        return self._exit_stack.enter_context(context_manager)

    def call_on_close(self, f: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        """Call ``f`` when this context closes. Can be used as decorator."""
        return self._exit_stack.callback(f)

    def close(self) -> None:
        """Run the close callbacks and exit the resources of this context."""
        self._exit_stack.close()
        self._exit_stack = ExitStack()

    @property
    def command_path(self) -> str:
        """The full invocation, such as ``cli sub`` for a subcommand. Used
        in usage lines. Arguments of parent commands are included.
        """
        rv = self.info_name or ""
        if self.parent is not None:
            parts = [self.parent.command_path]
            if isinstance(self.parent.command, Command):
                for param in self.parent.command.get_params(self):
                    parts.extend(param.get_usage_pieces(self))
            rv = f"{' '.join(parts)} {rv}"
        return rv.lstrip()

    def find_root(self) -> Context:
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def find_object(self, object_type: type[V]) -> V | None:
        """Find the closest :attr:`obj` of the given type, starting here."""
        node: Context | None = self
        while node is not None:
            if isinstance(node.obj, object_type):
                return node.obj
            node = node.parent
        return None

    def ensure_object(self, object_type: type[V]) -> V:
        """Like :meth:`find_object`, but create and store a new instance
        on this context if none is found.
        """
        rv = self.find_object(object_type)
        if rv is None:
            self.obj = rv = object_type()
        return rv

    @t.overload
    def lookup_default(self, name: str, call: t.Literal[True] = True) -> t.Any | None: ...

    @t.overload
    def lookup_default(
        self, name: str, call: t.Literal[False] = ...
    ) -> t.Any | t.Callable[[], t.Any] | None: ...

    def lookup_default(self, name: str, call: bool = True) -> t.Any | None:
        """Look up ``name`` in :attr:`default_map`, calling the value if it
        is callable and ``call`` is true.
        """
        if self.default_map is None:
            return None
        value = self.default_map.get(name)
        if call and callable(value):
            return value()
        return value

    def fail(self, message: str) -> t.NoReturn:
        """Abort with a usage error showing ``message``."""
        raise UsageError(message, self)

    def abort(self) -> t.NoReturn:
        raise Abort()

    def exit(self, code: int = 0) -> t.NoReturn:
        """Stop and exit with ``code``."""
        raise Exit(code)

    def get_usage(self) -> str:
        return self.command.get_usage(self)

    def get_help(self) -> str:
        return self.command.get_help(self)

    def _make_sub_context(self, command: Command) -> Context:
        return type(self)(command, info_name=command.name, parent=self)

    @t.overload
    def invoke(
        self, callback: t.Callable[..., V], /, *args: t.Any, **kwargs: t.Any
    ) -> V: ...

    @t.overload
    def invoke(self, callback: Command, /, *args: t.Any, **kwargs: t.Any) -> t.Any: ...

    def invoke(
        self, callback: Command | t.Callable[..., V], /, *args: t.Any, **kwargs: t.Any
    ) -> t.Any | V:
        """Call a function, or the callback of another command.

        For a command, a new sub context is created and parameters that
        were not passed as keyword arguments get their defaults.
        """
        if isinstance(callback, Command):
            other = callback
            if other.callback is None:
                raise TypeError(
                    "The given command does not have a callback that can be invoked."
                )
            func = other.callback
            ctx = self._make_sub_context(other)
            for param in other.params:
                if param.name not in kwargs and param.expose_value:
                    kwargs[param.name] = param.type_cast_value(  # type: ignore
                        ctx, param.get_default(ctx)
                    )
            # Record them so that a later ``forward`` passes them on.
            ctx.params.update(kwargs)
        else:
            func = callback
            ctx = self

        with augment_usage_errors(self):
            with ctx:
                return func(*args, **kwargs)

    def forward(self, cmd: Command, /, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Invoke another command with this context's parameters, overridden
        by ``kwargs``.
        """
        if not isinstance(cmd, Command):
            raise TypeError("Callback is not a command.")
        for name, value in self.params.items():
            kwargs.setdefault(name, value)
        return self.invoke(cmd, *args, **kwargs)

    def set_parameter_source(self, name: str, source: ParameterSource) -> None:
        self._parameter_source[name] = source

    def get_parameter_source(self, name: str) -> ParameterSource | None:
        """Where the value of parameter ``name`` came from, or ``None`` if
        it was not processed.
        """
        return self._parameter_source.get(name)


class Command:
    """A single command with parameters and a callback.

    :param name: the command name, used when it is part of a group.
    :param context_settings: default keyword arguments for its context.
    :param callback: called with the parameter values when invoked.
    :param params: the :class:`Option` and :class:`Argument` objects.
    :param help: help text, shown on the help page.
    :param epilog: text shown at the end of the help page.
    :param short_help: summary shown in the command list of a group.
    :param options_metavar: placeholder for options in the usage line.
    :param add_help_option: add a ``--help`` option.
    :param no_args_is_help: show help when called without arguments.
    :param hidden: hide the command from the help of its group.
    :param deprecated: mark as deprecated; a string gives the reason.
    """

    #: The class used for contexts of this command.
    context_class: type[Context] = Context

    #: Defaults for the matching context settings.
    allow_extra_args = False
    allow_interspersed_args = True
    ignore_unknown_options = False

    def __init__(
        self,
        name: str | None,
        context_settings: cabc.MutableMapping[str, t.Any] | None = None,
        callback: t.Callable[..., t.Any] | None = None,
        params: list[Parameter] | None = None,
        help: str | None = None,
        epilog: str | None = None,
        short_help: str | None = None,
        options_metavar: str | None = "[OPTIONS]",
        add_help_option: bool = True,
        no_args_is_help: bool = False,
        hidden: bool = False,
        deprecated: bool | str = False,
    ) -> None:
        self.name = name
        self.context_settings: cabc.MutableMapping[str, t.Any] = (
            {} if context_settings is None else context_settings
        )
        self.callback = callback
        self.params: list[Parameter] = params or []
        self.help = help
        self.epilog = epilog
        self.options_metavar = options_metavar
        self.short_help = short_help
        self.add_help_option = add_help_option
        self._help_option: Option | None = None
        self.no_args_is_help = no_args_is_help
        self.hidden = hidden
        self.deprecated = deprecated

    def to_info_dict(self, ctx: Context) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "params": [param.to_info_dict() for param in self.get_params(ctx)],
            "help": self.help,
            "epilog": self.epilog,
            "short_help": self.short_help,
            "hidden": self.hidden,
            "deprecated": self.deprecated,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"

    def get_usage(self, ctx: Context) -> str:
        formatter = ctx.make_formatter()
        self.format_usage(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def get_params(self, ctx: Context) -> list[Parameter]:
        """The parameters of this command, including the help option."""
        params = list(self.params)
        help_option = self.get_help_option(ctx)
        if help_option is not None:
            params.append(help_option)

        if __debug__:
            import warnings

            counts = Counter(opt for param in params for opt in param.opts)
            for opt, count in counts.items():
                if count > 1:
                    warnings.warn(
                        f"The parameter {opt} is used more than once. Remove its"
                        " duplicate as parameters should be unique.",
                        stacklevel=3,
                    )
        return params

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, " ".join(self.collect_usage_pieces(ctx)))

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        """The parts of the usage line after the command path."""
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        return pieces

    def get_help_option_names(self, ctx: Context) -> list[str]:
        """Help option names that no other parameter uses."""
        taken: set[str] = set()
        for param in self.params:
            taken.update(param.opts)
            taken.update(param.secondary_opts)
        return [name for name in ctx.help_option_names if name not in taken]

    def get_help_option(self, ctx: Context) -> Option | None:
        """The help option, or ``None`` if it is disabled. The object is
        cached so that processing order stays stable.
        """
        names = self.get_help_option_names(ctx)
        if not names or not self.add_help_option:
            return None
        if self._help_option is None:

            def show_help(ctx: Context, param: Parameter, value: bool) -> None:
                if not value or ctx.resilient_parsing:
                    return
                echo(ctx.get_help(), color=ctx.color)
                ctx.exit()

            self._help_option = Option(
                names,
                is_flag=True,
                is_eager=True,
                expose_value=False,
                callback=show_help,
                help=_("Show this message and exit."),
            )
        return self._help_option

    def make_parser(self, ctx: Context) -> _OptionParser:
        parser = _OptionParser(ctx)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser

    def get_help(self, ctx: Context) -> str:
        formatter = ctx.make_formatter()
        self.format_help(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def get_short_help_str(self, limit: int = 45) -> str:
        """Short summary: ``short_help`` if given, otherwise derived from
        the start of the help text.
        """
        if self.short_help:
            text = inspect.cleandoc(self.short_help)
        elif self.help:
            text = make_default_short_help(self.help, limit)
        else:
            text = ""
        if self.deprecated:
            text = _("{text} {deprecated_message}").format(
                text=text, deprecated_message=_deprecated_note(self.deprecated)
            )
        return text.strip()

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Write the help page: usage, help text, options, epilog."""
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_help_text(self, ctx: Context, formatter: HelpFormatter) -> None:
        # Text after a form feed is for documentation tools only.
        text = inspect.cleandoc(self.help).partition("\f")[0] if self.help else ""
        if self.deprecated:
            text = _("{text} {deprecated_message}").format(
                text=text, deprecated_message=_deprecated_note(self.deprecated)
            )
        if text:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(text)

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        records = []
        for param in self.get_params(ctx):
            record = param.get_help_record(ctx)
            if record is not None:
                records.append(record)
        if records:
            with formatter.section(_("Options")):
                formatter.write_dl(records)

    def format_epilog(self, ctx: Context, formatter: HelpFormatter) -> None:
        if self.epilog:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(inspect.cleandoc(self.epilog))

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: t.Any,
    ) -> Context:
        """Create a context and parse ``args`` into it, without invoking
        the command.
        """
        for key, value in self.context_settings.items():
            extra.setdefault(key, value)
        ctx = self.context_class(self, info_name=info_name, parent=parent, **extra)
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            raise NoArgsIsHelpError(ctx)

        parser = self.make_parser(ctx)
        opts, args, order = parser.parse_args(args=args)

        for param in iter_params_for_processing(order, self.get_params(ctx)):
            _, args = param.handle_parse_result(ctx, opts, args)

        if args and not ctx.allow_extra_args and not ctx.resilient_parsing:
            ctx.fail(
                ngettext(
                    "Got unexpected extra argument ({args})",
                    "Got unexpected extra arguments ({args})",
                    len(args),
                ).format(args=" ".join(map(str, args)))
            )

        ctx.args = args
        ctx._opt_prefixes.update(parser._opt_prefixes)
        return args

    def invoke(self, ctx: Context) -> t.Any:
        """Run the callback with the parsed parameters."""
        if self.deprecated:
            extra = f" {self.deprecated}" if isinstance(self.deprecated, str) else ""
            message = _(
                "DeprecationWarning: The command {name!r} is deprecated.{extra_message}"
            ).format(name=self.name, extra_message=extra)
            echo(style(message, fg="red"), err=True)

        if self.callback is not None:
            return ctx.invoke(self.callback, **ctx.params)
        return None

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        """Completions for an incomplete option name. Options that were
        already given are skipped unless they can be repeated.
        """
        from .shell_completion import CompletionItem

        results: list[CompletionItem] = []
        if incomplete and not incomplete[0].isalnum():
            for param in self.get_params(ctx):
                if not isinstance(param, Option) or param.hidden:
                    continue
                if (
                    not param.multiple
                    and ctx.get_parameter_source(param.name)  # type: ignore[arg-type]
                    is ParameterSource.COMMANDLINE
                ):
                    continue
                results.extend(
                    CompletionItem(name, help=param.help)
                    for name in [*param.opts, *param.secondary_opts]
                    if name.startswith(incomplete)
                )
        return results

    @t.overload
    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: t.Literal[True] = True,
        **extra: t.Any,
    ) -> t.NoReturn: ...

    @t.overload
    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = ...,
        **extra: t.Any,
    ) -> t.Any: ...

    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: t.Any,
    ) -> t.Any:
        """Run the command as a program.

        In standalone mode, errors are shown to the user and the process
        exits; otherwise exceptions propagate and the callback's return
        value is returned.

        :param args: arguments to parse, ``sys.argv[1:]`` by default.
        :param prog_name: program name for usage, detected by default.
        :param complete_var: environment variable that triggers shell
            completion, ``_<PROG_NAME>_COMPLETE`` by default.
        :param standalone_mode: handle errors and exit.
        :param windows_expand_args: expand globs and variables on Windows.
        :param extra: passed on to the context.
        """
        if args is None:
            args = sys.argv[1:]
            if os.name == "nt" and windows_expand_args:
                args = _expand_args(args)
        else:
            args = list(args)

        if prog_name is None:
            prog_name = _detect_program_name()

        self._main_shell_completion(extra, prog_name, complete_var)

        try:
            try:
                with self.make_context(prog_name, args, **extra) as ctx:
                    rv = self.invoke(ctx)
                    if not standalone_mode:
                        return rv
                    ctx.exit()
            except (EOFError, KeyboardInterrupt) as e:
                echo(file=sys.stderr)
                raise Abort() from e
            except ClickException as e:
                if not standalone_mode:
                    raise
                e.show()
                sys.exit(e.exit_code)
            except OSError as e:
                if e.errno != errno.EPIPE:
                    raise
                # The reader went away; silence further flush errors.
                sys.stdout = t.cast(t.TextIO, PacifyFlushWrapper(sys.stdout))
                sys.stderr = t.cast(t.TextIO, PacifyFlushWrapper(sys.stderr))
                sys.exit(1)
        except Exit as e:
            if standalone_mode:
                sys.exit(e.exit_code)
            return e.exit_code
        except Abort:
            if not standalone_mode:
                raise
            echo(_("Aborted!"), file=sys.stderr)
            sys.exit(1)

    def _main_shell_completion(
        self,
        ctx_args: cabc.MutableMapping[str, t.Any],
        prog_name: str,
        complete_var: str | None = None,
    ) -> None:
        """Answer a completion request from the shell if the completion
        environment variable is set, then exit.
        """
        if complete_var is None:
            complete_name = prog_name.replace("-", "_").replace(".", "_")
            complete_var = f"_{complete_name}_COMPLETE".upper()

        instruction = os.environ.get(complete_var)
        if not instruction:
            return

        from .shell_completion import shell_complete

        sys.exit(shell_complete(self, ctx_args, prog_name, complete_var, instruction))

    def __call__(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        return self.main(*args, **kwargs)


class _FakeSubclassCheck(type):
    def __subclasscheck__(cls, subclass: type) -> bool:
        return issubclass(subclass, cls.__bases__[0])

    def __instancecheck__(cls, instance: t.Any) -> bool:
        return isinstance(instance, cls.__bases__[0])


class _BaseCommand(Command, metaclass=_FakeSubclassCheck):
    """Deprecated alias of :class:`Command`."""


class Group(Command):
    """A command made of subcommands.

    :param commands: subcommands, as a mapping or a list.
    :param invoke_without_command: run the group callback even when no
        subcommand is given.
    :param no_args_is_help: show help when called without arguments.
        Defaults to the opposite of ``invoke_without_command``.
    :param subcommand_metavar: placeholder for the subcommand in usage.
    :param chain: allow several subcommands in one invocation.
    :param result_callback: called with the subcommand result(s).
    """

    allow_extra_args = True
    allow_interspersed_args = False

    #: Default ``cls`` for :meth:`command`.
    command_class: type[Command] | None = None

    #: Default ``cls`` for :meth:`group`. ``type`` means "the class of
    #: this group".
    group_class: type[Group] | type[type] | None = None

    def __init__(
        self,
        name: str | None = None,
        commands: cabc.MutableMapping[str, Command] | cabc.Sequence[Command] | None = None,
        invoke_without_command: bool = False,
        no_args_is_help: bool | None = None,
        subcommand_metavar: str | None = None,
        chain: bool = False,
        result_callback: t.Callable[..., t.Any] | None = None,
        **kwargs: t.Any,
    ) -> None:
        super().__init__(name, **kwargs)

        if commands is None:
            commands = {}
        elif isinstance(commands, cabc.Sequence):
            commands = {c.name: c for c in commands if c.name is not None}
        self.commands: cabc.MutableMapping[str, Command] = commands

        if no_args_is_help is None:
            no_args_is_help = not invoke_without_command
        self.no_args_is_help = no_args_is_help
        self.invoke_without_command = invoke_without_command

        if subcommand_metavar is None:
            if chain:
                subcommand_metavar = "COMMAND1 [ARGS]... [COMMAND2 [ARGS]...]..."
            else:
                subcommand_metavar = "COMMAND [ARGS]..."
        self.subcommand_metavar = subcommand_metavar
        self.chain = chain
        self._result_callback = result_callback

        if self.chain:
            for param in self.params:
                if isinstance(param, Argument) and not param.required:
                    raise RuntimeError(
                        "A group in chain mode cannot have optional arguments."
                    )

    def to_info_dict(self, ctx: Context) -> dict[str, t.Any]:
        info = super().to_info_dict(ctx)
        commands = {}
        with ctx.scope(cleanup=False):
            for name in self.list_commands(ctx):
                command = self.get_command(ctx, name)
                if command is None:
                    continue
                sub_ctx = ctx._make_sub_context(command)
                with sub_ctx.scope(cleanup=False):
                    commands[name] = command.to_info_dict(sub_ctx)
        info.update(commands=commands, chain=self.chain)
        return info

    def add_command(self, cmd: Command, name: str | None = None) -> None:
        """Register a subcommand under ``name`` or its own name."""
        name = name or cmd.name
        if name is None:
            raise TypeError("Command has no name.")
        _check_nested_chain(self, name, cmd, register=True)
        self.commands[name] = cmd

    def _decorate(
        self, factory_name: str, cls_attr: str, args: tuple[t.Any, ...], kwargs: dict[str, t.Any]
    ) -> t.Any:
        from . import decorators

        factory = getattr(decorators, factory_name)
        func: t.Callable[..., t.Any] | None = None
        if args and callable(args[0]):
            assert len(args) == 1 and not kwargs, (
                f"Use '{factory_name}(**kwargs)(callable)' to provide arguments."
            )
            (func,) = args
            args = ()

        default_cls = getattr(self, cls_attr)
        if default_cls is not None and kwargs.get("cls") is None:
            kwargs["cls"] = type(self) if default_cls is type else default_cls

        def decorator(f: t.Callable[..., t.Any]) -> Command:
            cmd = factory(*args, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        if func is not None:
            return decorator(func)
        return decorator

    def command(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Like :func:`click.command`, but also adds the command to this
        group. :attr:`command_class` is used unless ``cls`` is given.
        """
        return self._decorate("command", "command_class", args, kwargs)

    def group(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Like :func:`click.group`, but also adds the group to this group.
        :attr:`group_class` is used unless ``cls`` is given.
        """
        return self._decorate("group", "group_class", args, kwargs)

    def result_callback(self, replace: bool = False) -> t.Callable[[F], F]:
        """Register a function that receives the return value of the
        subcommand (a list of values for chained groups) along with the
        group's own parameters. Registering a second one wraps the first,
        unless ``replace`` is true.
        """

        def decorator(f: F) -> F:
            previous = self._result_callback
            if previous is None or replace:
                self._result_callback = f
                return f

            def combined(value: t.Any, /, *args: t.Any, **kwargs: t.Any) -> t.Any:
                return f(previous(value, *args, **kwargs), *args, **kwargs)

            self._result_callback = rv = update_wrapper(t.cast(F, combined), f)
            return rv

        return decorator

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        return self.commands.get(cmd_name)

    def list_commands(self, ctx: Context) -> list[str]:
        return sorted(self.commands)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = super().collect_usage_pieces(ctx)
        pieces.append(self.subcommand_metavar)
        return pieces

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        super().format_options(ctx, formatter)
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Write the list of visible subcommands with their summaries."""
        visible = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            visible.append((name, cmd))
        if not visible:
            return

        limit = formatter.width - 6 - max(len(name) for name, _ in visible)
        rows = [(name, cmd.get_short_help_str(limit)) for name, cmd in visible]
        with formatter.section(_("Commands")):
            formatter.write_dl(rows)

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            raise NoArgsIsHelpError(ctx)

        rest = super().parse_args(ctx, args)
        if self.chain:
            ctx._protected_args = rest
            ctx.args = []
        elif rest:
            ctx._protected_args, ctx.args = rest[:1], rest[1:]
        return ctx.args

    def invoke(self, ctx: Context) -> t.Any:
        def finish(value: t.Any) -> t.Any:
            if self._result_callback is not None:
                value = ctx.invoke(self._result_callback, value, **ctx.params)
            return value

        if not ctx._protected_args:
            if self.invoke_without_command:
                with ctx:
                    rv = super().invoke(ctx)
                    return finish([] if self.chain else rv)
            ctx.fail(_("Missing command."))

        args = [*ctx._protected_args, *ctx.args]
        ctx.args = []
        ctx._protected_args = []

        if not self.chain:
            with ctx:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                assert cmd is not None
                ctx.invoked_subcommand = cmd_name
                super().invoke(ctx)
                sub_ctx = cmd.make_context(cmd_name, args, parent=ctx)
                with sub_ctx:
                    return finish(sub_ctx.command.invoke(sub_ctx))

        # Chain mode: parse all subcommands first so that usage errors are
        # reported before anything runs, then invoke them in order.
        with ctx:
            ctx.invoked_subcommand = "*" if args else None
            super().invoke(ctx)

            contexts = []
            while args:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                assert cmd is not None
                sub_ctx = cmd.make_context(
                    cmd_name,
                    args,
                    parent=ctx,
                    allow_extra_args=True,
                    allow_interspersed_args=False,
                )
                contexts.append(sub_ctx)
                args, sub_ctx.args = sub_ctx.args, []

            results = []
            for sub_ctx in contexts:
                with sub_ctx:
                    results.append(sub_ctx.command.invoke(sub_ctx))
            return finish(results)

    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command | None, list[str]]:
        """Find the subcommand named by the first argument."""
        original = make_str(args[0])
        cmd_name = original
        cmd = self.get_command(ctx, cmd_name)

        if cmd is None and ctx.token_normalize_func is not None:
            cmd_name = ctx.token_normalize_func(cmd_name)
            cmd = self.get_command(ctx, cmd_name)

        if cmd is None and not ctx.resilient_parsing:
            if _split_opt(cmd_name)[0]:
                # Looks like an option; let the parser produce the error.
                self.parse_args(ctx, args)
            ctx.fail(_("No such command {name!r}.").format(name=original))

        return (cmd_name if cmd else None), cmd, args[1:]

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        from .shell_completion import CompletionItem

        results = [
            CompletionItem(name, help=command.get_short_help_str())
            for name, command in _complete_visible_commands(ctx, incomplete)
        ]
        results.extend(super().shell_complete(ctx, incomplete))
        return results


class _MultiCommand(Group, metaclass=_FakeSubclassCheck):
    """Deprecated alias of :class:`Group`."""


class CommandCollection(Group):
    """A group that also offers the commands of other groups.

    :param sources: groups whose commands are made available.
    """

    def __init__(
        self,
        name: str | None = None,
        sources: list[Group] | None = None,
        **kwargs: t.Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self.sources: list[Group] = sources or []

    def add_source(self, group: Group) -> None:
        self.sources.append(group)

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        rv = self.commands.get(cmd_name)
        if rv is not None:
            return rv
        for source in self.sources:
            rv = source.get_command(ctx, cmd_name)
            if rv is not None:
                if self.chain:
                    _check_nested_chain(self, cmd_name, rv)
                return rv
        return None

    def list_commands(self, ctx: Context) -> list[str]:
        names = set(self.commands)
        for source in self.sources:
            names.update(source.list_commands(ctx))
        return sorted(names)


def _check_iter(value: t.Any) -> cabc.Iterator[t.Any]:
    """Iterate over ``value``, refusing strings."""
    if isinstance(value, str):
        raise TypeError
    return iter(value)


class Parameter:
    """Base class for :class:`Option` and :class:`Argument`.

    :param param_decls: names and flags, such as ``("-f", "--foo")``.
    :param type: the :class:`ParamType` or a Python type; guessed from the
        default if not given.
    :param required: the value must be provided.
    :param default: value used when none is given; may be a callable.
    :param callback: ``f(ctx, param, value)`` that may validate or replace
        the converted value.
    :param nargs: number of values; ``-1`` for any number (arguments only).
    :param multiple: may be given several times, collecting a tuple.
    :param metavar: placeholder shown in help.
    :param expose_value: pass the value to the command callback.
    :param is_eager: process before other parameters.
    :param envvar: environment variable name(s) to read the value from.
    :param shell_complete: ``f(ctx, param, incomplete)`` returning
        completions.
    :param deprecated: warn when used; a string gives the reason.
    """

    param_type_name = "parameter"

    def __init__(
        self,
        param_decls: cabc.Sequence[str] | None = None,
        type: types.ParamType | t.Any | None = None,
        required: bool = False,
        default: t.Any | t.Callable[[], t.Any] | None = None,
        callback: t.Callable[[Context, Parameter, t.Any], t.Any] | None = None,
        nargs: int | None = None,
        multiple: bool = False,
        metavar: str | None = None,
        expose_value: bool = True,
        is_eager: bool = False,
        envvar: str | cabc.Sequence[str] | None = None,
        shell_complete: t.Callable[
            [Context, Parameter, str], list[CompletionItem] | list[str]
        ]
        | None = None,
        deprecated: bool | str = False,
    ) -> None:
        self.name: str | None
        self.opts: list[str]
        self.secondary_opts: list[str]
        self.name, self.opts, self.secondary_opts = self._parse_decls(
            param_decls or (), expose_value
        )
        self.type: types.ParamType = types.convert_type(type, default)

        if nargs is None:
            nargs = self.type.arity if self.type.is_composite else 1

        self.required = required
        self.callback = callback
        self.nargs = nargs
        self.multiple = multiple
        self.expose_value = expose_value
        self.default = default
        self.is_eager = is_eager
        self.metavar = metavar
        self.envvar = envvar
        self._custom_shell_complete = shell_complete
        self.deprecated = deprecated

        if __debug__:
            self._validate_settings(default)

    def _validate_settings(self, default: t.Any) -> None:
        nargs = self.nargs
        if self.type.is_composite and nargs != self.type.arity:
            raise ValueError(
                f"'nargs' must be {self.type.arity} (or None) for"
                f" type {self.type!r}, but it was {nargs}."
            )

        sample = None if callable(default) else default
        if sample is not None and self.multiple:
            try:
                sample = next(_check_iter(sample), None)
            except TypeError:
                raise ValueError(
                    "'default' must be a list when 'multiple' is true."
                ) from None

        if sample is not None and nargs != 1:
            try:
                _check_iter(sample)
            except TypeError:
                if self.multiple:
                    message = (
                        "'default' must be a list of lists when 'multiple' is"
                        " true and 'nargs' != 1."
                    )
                else:
                    message = "'default' must be a list when 'nargs' != 1."
                raise ValueError(message) from None

            if nargs > 1 and len(sample) != nargs:
                subject = "item length" if self.multiple else "length"
                raise ValueError(f"'default' {subject} must match nargs={nargs}.")

        if self.required and self.deprecated:
            raise ValueError(
                f"The {self.param_type_name} '{self.human_readable_name}' is"
                " deprecated and still required. A deprecated"
                f" {self.param_type_name} cannot be required."
            )

    def to_info_dict(self) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "param_type_name": self.param_type_name,
            "opts": self.opts,
            "secondary_opts": self.secondary_opts,
            "type": self.type.to_info_dict(),
            "required": self.required,
            "nargs": self.nargs,
            "multiple": self.multiple,
            "default": self.default,
            "envvar": self.envvar,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"

    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str | None, list[str], list[str]]:
        raise NotImplementedError()

    @property
    def human_readable_name(self) -> str:
        """Name used in error messages."""
        return self.name  # type: ignore

    def make_metavar(self, ctx: Context) -> str:
        if self.metavar is not None:
            return self.metavar
        metavar = self.type.get_metavar(param=self, ctx=ctx)
        if metavar is None:
            metavar = self.type.name.upper()
        if self.nargs != 1:
            metavar += "..."
        return metavar

    @t.overload
    def get_default(self, ctx: Context, call: t.Literal[True] = True) -> t.Any | None: ...

    @t.overload
    def get_default(
        self, ctx: Context, call: bool = ...
    ) -> t.Any | t.Callable[[], t.Any] | None: ...

    def get_default(
        self, ctx: Context, call: bool = True
    ) -> t.Any | t.Callable[[], t.Any] | None:
        """The default value, taken from the context's ``default_map``
        first. Callable defaults are called unless ``call`` is false.
        """
        value = ctx.lookup_default(self.name, call=False)  # type: ignore[arg-type]
        if value is None:
            value = self.default
        if call and callable(value):
            value = value()
        return value

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:
        raise NotImplementedError()

    def consume_value(
        self, ctx: Context, opts: cabc.Mapping[str, t.Any]
    ) -> tuple[t.Any, ParameterSource]:
        """Find the raw value: command line, then environment, then
        ``default_map``, then the default.
        """
        value = opts.get(self.name)  # type: ignore[arg-type]
        source = ParameterSource.COMMANDLINE

        if value is None:
            value = self.value_from_envvar(ctx)
            source = ParameterSource.ENVIRONMENT
        if value is None:
            value = ctx.lookup_default(self.name)  # type: ignore[arg-type]
            source = ParameterSource.DEFAULT_MAP
        if value is None:
            value = self.get_default(ctx)
            source = ParameterSource.DEFAULT
        return value, source

    def type_cast_value(self, ctx: Context, value: t.Any) -> t.Any:
        """Convert ``value`` with the parameter type, taking ``nargs`` and
        ``multiple`` into account.
        """
        if value is None:
            return () if self.multiple or self.nargs == -1 else None

        def as_iter(v: t.Any) -> cabc.Iterator[t.Any]:
            try:
                return _check_iter(v)
            except TypeError:
                raise BadParameter(
                    _("Value must be an iterable."), ctx=ctx, param=self
                ) from None

        def convert_one(v: t.Any) -> t.Any:
            return self.type(v, param=self, ctx=ctx)

        def convert_many(v: t.Any) -> tuple[t.Any, ...]:
            return tuple(self.type(x, self, ctx) for x in as_iter(v))

        def convert_exact(v: t.Any) -> tuple[t.Any, ...]:
            items = tuple(as_iter(v))
            if len(items) != self.nargs:
                raise BadParameter(
                    ngettext(
                        "Takes {nargs} values but 1 was given.",
                        "Takes {nargs} values but {len} were given.",
                        len(items),
                    ).format(nargs=self.nargs, len=len(items)),
                    ctx=ctx,
                    param=self,
                )
            return tuple(self.type(x, self, ctx) for x in items)

        if self.nargs == 1 or self.type.is_composite:
            convert: t.Callable[[t.Any], t.Any] = convert_one
        elif self.nargs == -1:
            convert = convert_many
        else:
            convert = convert_exact

        if self.multiple:
            return tuple(convert(item) for item in as_iter(value))
        return convert(value)

    def value_is_missing(self, value: t.Any) -> bool:
        if value is None:
            return True
        if (self.nargs != 1 or self.multiple) and value == ():
            return True
        return False

    def process_value(self, ctx: Context, value: t.Any) -> t.Any:
        """Convert, check that required values are present, and run the
        callback.
        """
        value = self.type_cast_value(ctx, value)
        if self.required and self.value_is_missing(value):
            raise MissingParameter(ctx=ctx, param=self)
        if self.callback is not None:
            value = self.callback(ctx, self, value)
        return value

    def resolve_envvar_value(self, ctx: Context) -> str | None:
        """The first non-empty value of the configured environment
        variable(s).
        """
        if self.envvar is None:
            return None
        names = [self.envvar] if isinstance(self.envvar, str) else self.envvar
        for name in names:
            rv = os.environ.get(name)
            if rv:
                return rv
        return None

    def value_from_envvar(self, ctx: Context) -> t.Any | None:
        rv: t.Any | None = self.resolve_envvar_value(ctx)
        if rv is not None and self.nargs != 1:
            rv = self.type.split_envvar_value(rv)
        return rv

    def handle_parse_result(
        self, ctx: Context, opts: cabc.Mapping[str, t.Any], args: list[str]
    ) -> tuple[t.Any, list[str]]:
        """Resolve, process and store this parameter's value."""
        with augment_usage_errors(ctx, param=self):
            value, source = self.consume_value(ctx, opts)

            if self.deprecated and source is ParameterSource.COMMANDLINE:
                extra = f" {self.deprecated}" if isinstance(self.deprecated, str) else ""
                message = _(
                    "DeprecationWarning: The {param_type} {name!r} is deprecated."
                    "{extra_message}"
                ).format(
                    param_type=self.param_type_name,
                    name=self.human_readable_name,
                    extra_message=extra,
                )
                echo(style(message, fg="red"), err=True)

            ctx.set_parameter_source(self.name, source)  # type: ignore[arg-type]

            try:
                value = self.process_value(ctx, value)
            except Exception:
                if not ctx.resilient_parsing:
                    raise
                value = None

        if self.expose_value:
            ctx.params[self.name] = value  # type: ignore[index]
        return value, args

    def get_help_record(self, ctx: Context) -> tuple[str, str] | None:
        return None

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return []

    def get_error_hint(self, ctx: Context) -> str:
        """How the parameter is named in error messages."""
        names = self.opts or [self.human_readable_name]
        return " / ".join(f"'{name}'" for name in names)

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        """Completions for a value of this parameter: the custom function
        if one was given, otherwise the type's completions.
        """
        if self._custom_shell_complete is not None:
            results = self._custom_shell_complete(ctx, self, incomplete)
            if results and isinstance(results[0], str):
                from .shell_completion import CompletionItem

                results = [CompletionItem(c) for c in results]
            return t.cast("list[CompletionItem]", results)
        return self.type.shell_complete(ctx, self, incomplete)


class Option(Parameter):
    """A named parameter such as ``--name value`` or ``-v``.

    :param show_default: show the default in help; a string is shown
        instead of the actual value.
    :param prompt: ask for the value when not given; ``True`` derives the
        prompt text from the name.
    :param confirmation_prompt: ask twice and compare; a string sets the
        second prompt's text.
    :param prompt_required: when false, only prompt if the option is
        given without a value.
    :param hide_input: hide typed input when prompting.
    :param is_flag: the option takes no value.
    :param flag_value: value stored when the flag is given.
    :param multiple: may be repeated, collecting a tuple.
    :param count: count how often the flag is given.
    :param allow_from_autoenv: read from the automatic environment
        variable if the context has a prefix.
    :param help: help text.
    :param hidden: hide from help.
    :param show_choices: show choices in prompts.
    :param show_envvar: show the environment variable in help.
    """

    param_type_name = "option"

    def __init__(
        self,
        param_decls: cabc.Sequence[str] | None = None,
        show_default: bool | str | None = None,
        prompt: bool | str = False,
        confirmation_prompt: bool | str = False,
        prompt_required: bool = True,
        hide_input: bool = False,
        is_flag: bool | None = None,
        flag_value: t.Any | None = None,
        multiple: bool = False,
        count: bool = False,
        allow_from_autoenv: bool = True,
        type: types.ParamType | t.Any | None = None,
        help: str | None = None,
        hidden: bool = False,
        show_choices: bool = True,
        show_envvar: bool = False,
        deprecated: bool | str = False,
        **attrs: t.Any,
    ) -> None:
        if help:
            help = inspect.cleandoc(help)

        default_given = "default" in attrs
        super().__init__(
            param_decls, type=type, multiple=multiple, deprecated=deprecated, **attrs
        )

        if prompt is True:
            if self.name is None:
                raise TypeError("'name' is required with 'prompt=True'.")
            prompt_text: str | None = self.name.replace("_", " ").capitalize()
        elif prompt is False:
            prompt_text = None
        else:
            prompt_text = prompt

        if deprecated:
            note = _deprecated_note(deprecated)
            help = f"{help} {note}" if help is not None else note

        self.prompt = prompt_text
        self.confirmation_prompt = confirmation_prompt
        self.prompt_required = prompt_required
        self.hide_input = hide_input
        self.hidden = hidden

        # An option with a prompt that is only shown when the option is
        # given without a value: ``--name`` prompts, ``--name x`` does not.
        self._flag_needs_value = self.prompt is not None and not self.prompt_required

        if is_flag is None:
            if flag_value is not None:
                is_flag = True
            elif self._flag_needs_value:
                is_flag = False
            else:
                is_flag = bool(self.secondary_opts)
        elif is_flag is False and not self._flag_needs_value:
            # ``is_flag=False`` with a ``flag_value`` makes the value
            # optional: given alone, the option stores ``flag_value``.
            self._flag_needs_value = flag_value is not None

        if is_flag and not default_given and not self.required:
            self.default = () if multiple else False

        if is_flag and flag_value is None:
            flag_value = not self.default

        if is_flag and type is None:
            self.type = types.convert_type(None, flag_value)

        self.is_flag: bool = bool(is_flag)
        self.is_bool_flag: bool = self.is_flag and isinstance(
            self.type, types.BoolParamType
        )
        self.flag_value: t.Any = flag_value

        self.count = count
        if count:
            if type is None:
                self.type = types.IntRange(min=0)
            if not default_given:
                self.default = 0

        self.allow_from_autoenv = allow_from_autoenv
        self.help = help
        self.show_default = show_default
        self.show_choices = show_choices
        self.show_envvar = show_envvar

        if __debug__:
            if deprecated and prompt:
                raise ValueError("`deprecated` options cannot use `prompt`.")
            if self.nargs == -1:
                raise TypeError("nargs=-1 is not supported for options.")
            if self.prompt and self.is_flag and not self.is_bool_flag:
                raise TypeError("'prompt' is not valid for non-boolean flag.")
            if not self.is_bool_flag and self.secondary_opts:
                raise TypeError("Secondary flag is not valid for non-boolean flag.")
            if self.is_bool_flag and self.hide_input and self.prompt is not None:
                raise TypeError(
                    "'prompt' with 'hide_input' is not valid for boolean flag."
                )
            if self.count:
                if self.multiple:
                    raise TypeError("'count' is not valid with 'multiple'.")
                if self.is_flag:
                    raise TypeError("'count' is not valid with 'is_flag'.")

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info.update(
            help=self.help,
            prompt=self.prompt,
            is_flag=self.is_flag,
            flag_value=self.flag_value,
            count=self.count,
            hidden=self.hidden,
        )
        return info

    def get_error_hint(self, ctx: Context) -> str:
        names = self.opts or [self.human_readable_name]
        return " / ".join(f"'{name}'" for name in names)

    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str | None, list[str], list[str]]:
        opts: list[str] = []
        secondary_opts: list[str] = []
        name: str | None = None
        candidates: list[tuple[str, str]] = []

        for decl in decls:
            if decl.isidentifier():
                if name is not None:
                    raise TypeError(f"Name '{name}' defined twice")
                name = decl
                continue

            # ``--flag/--no-flag`` declares an on and an off switch. If the
            # options themselves use ``/`` as prefix, ``;`` separates them.
            sep = ";" if decl[:1] == "/" else "/"
            if sep in decl:
                first, second = decl.split(sep, 1)
                first = first.rstrip()
                if first:
                    candidates.append(_split_opt(first))
                    opts.append(first)
                second = second.lstrip()
                if second:
                    secondary_opts.append(second)
                if first == second:
                    raise ValueError(
                        f"Boolean option {decl!r} cannot use the"
                        " same flag for true/false."
                    )
            else:
                candidates.append(_split_opt(decl))
                opts.append(decl)

        if name is None and candidates:
            # Prefer the name of the option with the longest prefix, so
            # ``-f, --foo`` is named ``foo``.
            candidates.sort(key=lambda c: -len(c[0]))
            name = candidates[0][1].replace("-", "_").lower()
            if not name.isidentifier():
                name = None

        if name is None:
            if not expose_value:
                return None, opts, secondary_opts
            raise TypeError(
                f"Could not determine name for option with declarations {decls!r}"
            )

        if not opts and not secondary_opts:
            raise TypeError(
                f"No options defined but a name was passed ({name})."
                " Did you mean to declare an argument instead? Did"
                f" you mean to pass '--{name}'?"
            )

        return name, opts, secondary_opts

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:
        if self.multiple:
            action = "append"
        elif self.count:
            action = "count"
        else:
            action = "store"

        if not self.is_flag:
            parser.add_option(
                obj=self, opts=self.opts, dest=self.name, action=action, nargs=self.nargs
            )
            return

        action = f"{action}_const"
        if self.is_bool_flag and self.secondary_opts:
            parser.add_option(
                obj=self, opts=self.opts, dest=self.name, action=action, const=True
            )
            parser.add_option(
                obj=self,
                opts=self.secondary_opts,
                dest=self.name,
                action=action,
                const=False,
            )
        else:
            parser.add_option(
                obj=self,
                opts=self.opts,
                dest=self.name,
                action=action,
                const=self.flag_value,
            )

    def get_help_record(self, ctx: Context) -> tuple[str, str] | None:
        if self.hidden:
            return None

        uses_slash = False

        def render(opts: cabc.Sequence[str]) -> str:
            nonlocal uses_slash
            text, slash = join_options(opts)
            uses_slash = uses_slash or slash
            if not self.is_flag and not self.count:
                text += f" {self.make_metavar(ctx)}"
            return text

        names = [render(self.opts)]
        if self.secondary_opts:
            names.append(render(self.secondary_opts))

        help = self.help or ""
        extra = self.get_help_extra(ctx)
        items = []
        if "envvars" in extra:
            items.append(_("env var: {var}").format(var=", ".join(extra["envvars"])))
        if "default" in extra:
            items.append(_("default: {default}").format(default=extra["default"]))
        if "range" in extra:
            items.append(extra["range"])
        if "required" in extra:
            items.append(_(extra["required"]))

        if items:
            joined = "; ".join(items)
            help = f"{help}  [{joined}]" if help else f"[{joined}]"

        return ("; " if uses_slash else " / ").join(names), help

    def get_help_extra(self, ctx: Context) -> types.OptionHelpExtra:
        """The bracketed details shown after the help text: environment
        variable, default, range and whether the option is required.
        """
        extra: types.OptionHelpExtra = {}

        if self.show_envvar:
            envvar = self.envvar
            if (
                envvar is None
                and self.allow_from_autoenv
                and ctx.auto_envvar_prefix is not None
                and self.name is not None
            ):
                envvar = f"{ctx.auto_envvar_prefix}_{self.name.upper()}"
            if envvar is not None:
                extra["envvars"] = (envvar,) if isinstance(envvar, str) else tuple(envvar)

        # Looking up the default must not fail while rendering help.
        resilient = ctx.resilient_parsing
        ctx.resilient_parsing = True
        try:
            default_value = self.get_default(ctx, call=False)
        finally:
            ctx.resilient_parsing = resilient

        show_default: bool | str | None = self.show_default
        if show_default is None:
            show_default = ctx.show_default
        custom_text = isinstance(show_default, str)

        if custom_text or (show_default and default_value is not None):
            if custom_text:
                text = f"({show_default})"
            elif isinstance(default_value, (list, tuple)):
                text = ", ".join(str(d) for d in default_value)
            elif isinstance(default_value, enum.Enum):
                text = default_value.name
            elif inspect.isfunction(default_value):
                text = _("(dynamic)")
            elif self.is_bool_flag and self.secondary_opts:
                # Show which of the switches is on by default.
                side = self.opts if default_value else self.secondary_opts
                text = _split_opt(side[0])[1]
            elif self.is_bool_flag and not self.secondary_opts and not default_value:
                text = ""
            elif default_value == "":
                text = '""'
            else:
                text = str(default_value)

            if text:
                extra["default"] = text

        if isinstance(self.type, types._NumberRangeBase) and not (
            self.count and self.type.min == 0 and self.type.max is None
        ):
            described = self.type._describe_range()
            if described:
                extra["range"] = described

        if self.required:
            extra["required"] = "required"

        return extra

    @t.overload
    def get_default(self, ctx: Context, call: t.Literal[True] = True) -> t.Any | None: ...

    @t.overload
    def get_default(
        self, ctx: Context, call: bool = ...
    ) -> t.Any | t.Callable[[], t.Any] | None: ...

    def get_default(
        self, ctx: Context, call: bool = True
    ) -> t.Any | t.Callable[[], t.Any] | None:
        # Several non-boolean flags may share one name (feature switches).
        # The one marked with a default provides its flag value.
        if self.is_flag and not self.is_bool_flag:
            for param in ctx.command.params:
                if param.name == self.name and param.default:
                    return t.cast(Option, param).flag_value
            return None
        return super().get_default(ctx, call=call)

    def prompt_for_value(self, ctx: Context) -> t.Any:
        """Ask the user for the value."""
        assert self.prompt is not None
        default = self.get_default(ctx)

        if self.is_bool_flag:
            return confirm(self.prompt, default)

        return prompt(
            self.prompt,
            default=default,
            type=self.type,
            hide_input=self.hide_input,
            show_choices=self.show_choices,
            confirmation_prompt=self.confirmation_prompt,
            value_proc=lambda x: self.process_value(ctx, x),
        )

    def resolve_envvar_value(self, ctx: Context) -> str | None:
        rv = super().resolve_envvar_value(ctx)
        if rv is not None:
            return rv
        if (
            self.allow_from_autoenv
            and ctx.auto_envvar_prefix is not None
            and self.name is not None
        ):
            rv = os.environ.get(f"{ctx.auto_envvar_prefix}_{self.name.upper()}")
            if rv:
                return rv
        return None

    def value_from_envvar(self, ctx: Context) -> t.Any | None:
        rv: t.Any | None = self.resolve_envvar_value(ctx)
        if rv is None:
            return None
        if self.nargs != 1 or self.multiple:
            rv = self.type.split_envvar_value(rv)
            if self.multiple and self.nargs != 1:
                rv = batch(rv, self.nargs)
        return rv

    def consume_value(
        self, ctx: Context, opts: cabc.Mapping[str, Parameter]
    ) -> tuple[t.Any, ParameterSource]:
        value, source = super().consume_value(ctx, opts)

        if value is _flag_needs_value:
            # Given without a value: prompt, or use the flag value.
            if self.prompt is not None and not ctx.resilient_parsing:
                value = self.prompt_for_value(ctx)
                source = ParameterSource.PROMPT
            else:
                value = self.flag_value
        elif (
            self.multiple
            and value is not None
            and any(v is _flag_needs_value for v in value)
        ):
            value = [self.flag_value if v is _flag_needs_value else v for v in value]
            source = ParameterSource.COMMANDLINE
        elif (
            source in {None, ParameterSource.DEFAULT}
            and self.prompt is not None
            and (self.required or self.prompt_required)
            and not ctx.resilient_parsing
        ):
            value = self.prompt_for_value(ctx)
            source = ParameterSource.PROMPT

        return value, source


class Argument(Parameter):
    """A positional parameter. Arguments are required by default unless
    they have a default or take a variable number of values.
    """

    param_type_name = "argument"

    def __init__(
        self,
        param_decls: cabc.Sequence[str],
        required: bool | None = None,
        **attrs: t.Any,
    ) -> None:
        if required is None:
            if attrs.get("default") is not None:
                required = False
            else:
                required = attrs.get("nargs", 1) > 0

        if "multiple" in attrs:
            raise TypeError("__init__() got an unexpected keyword argument 'multiple'.")

        super().__init__(param_decls, required=required, **attrs)

        if __debug__:
            if self.default is not None and self.nargs == -1:
                raise TypeError("'default' is not supported for nargs=-1.")

    @property
    def human_readable_name(self) -> str:
        if self.metavar is not None:
            return self.metavar
        return self.name.upper()  # type: ignore

    def make_metavar(self, ctx: Context) -> str:
        if self.metavar is not None:
            return self.metavar
        var = self.type.get_metavar(param=self, ctx=ctx)
        if not var:
            var = self.name.upper()  # type: ignore
        if self.deprecated:
            var += "!"
        if not self.required:
            var = f"[{var}]"
        if self.nargs != 1:
            var += "..."
        return var

    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str | None, list[str], list[str]]:
        if not decls:
            if not expose_value:
                return None, [], []
            raise TypeError("Argument is marked as exposed, but does not have a name.")
        if len(decls) != 1:
            raise TypeError(
                "Arguments take exactly one parameter declaration, got"
                f" {len(decls)}: {decls}."
            )
        decl = decls[0]
        return decl.replace("-", "_").lower(), [decl], []

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return [self.make_metavar(ctx)]

    def get_error_hint(self, ctx: Context) -> str:
        return f"'{self.make_metavar(ctx)}'"

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:
        parser.add_argument(dest=self.name, nargs=self.nargs, obj=self)


def __getattr__(name: str) -> object:
    import warnings

    if name == "BaseCommand":
        warnings.warn(
            "'BaseCommand' is deprecated and will be removed in Click 9.0. Use"
            " 'Command' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _BaseCommand

    if name == "MultiCommand":
        warnings.warn(
            "'MultiCommand' is deprecated and will be removed in Click 9.0. Use"
            " 'Group' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _MultiCommand

    raise AttributeError(name)
