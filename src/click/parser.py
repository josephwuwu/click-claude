"""Turns a list of command line tokens into option values and positional
arguments.

The parser only knows about option strings, how many values each one takes
and what to do with them. Type conversion, defaults, prompting and so on are
handled by the parameter objects in :mod:`click.core`.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t
from gettext import gettext as _
from gettext import ngettext

from ._utils import FLAG_NEEDS_VALUE
from .exceptions import BadArgumentUsage
from .exceptions import BadOptionUsage
from .exceptions import NoSuchOption
from .exceptions import UsageError

if t.TYPE_CHECKING:
    from .core import Argument as CoreArgument
    from .core import Context
    from .core import Option as CoreOption
    from .core import Parameter as CoreParameter

V = t.TypeVar("V")


# Stored for an option with an optional value that was given without one.
_flag_needs_value: t.Any = FLAG_NEEDS_VALUE


def _split_opt(opt: str) -> tuple[str, str]:
    """Split an option string into its prefix and name, for example
    ``"--foo"`` into ``("--", "foo")``.
    """
    first = opt[:1]
    if first.isalnum():
        return "", opt
    if opt[1:2] == first:
        return opt[:2], opt[2:]
    return first, opt[1:]


def _normalize_opt(opt: str, ctx: Context | None) -> str:
    if ctx is None or ctx.token_normalize_func is None:
        return opt
    prefix, name = _split_opt(opt)
    return f"{prefix}{ctx.token_normalize_func(name)}"


def _unpack_args(
    args: cabc.Sequence[str], nargs_spec: cabc.Sequence[int]
) -> tuple[tuple[t.Any, ...], list[str]]:
    """Distribute ``args`` over positional parameters described by
    ``nargs_spec``.

    At most one entry may be ``-1``, meaning "everything that is left". The
    parameters before it take values from the front, the ones after it from
    the back. Missing values are filled with ``None``. Returns the values for
    each parameter and the arguments nobody took.
    """
    pool = list(args)
    spec = list(nargs_spec)

    greedy = [i for i, n in enumerate(spec) if n < 0]
    if len(greedy) > 1:
        raise TypeError("Cannot have two nargs < 0")

    def take(count: int, from_end: bool) -> list[t.Any]:
        taken: list[t.Any] = []
        for _index in range(count):
            if not pool:
                taken.append(None)
            elif from_end:
                taken.append(pool.pop())
            else:
                taken.append(pool.pop(0))
        if from_end:
            taken.reverse()
        return taken

    def value_for(n: int, from_end: bool) -> t.Any:
        if n == 1:
            return take(1, from_end)[0]
        return tuple(take(n, from_end))

    if not greedy:
        values = [value_for(n, False) for n in spec]
        return tuple(values), pool

    split = greedy[0]
    front = [value_for(n, False) for n in spec[:split]]
    back = [value_for(n, True) for n in reversed(spec[split + 1 :])]
    back.reverse()
    middle = tuple(pool)
    return tuple(front + [middle] + back), []


class _Option:
    def __init__(
        self,
        obj: CoreOption,
        opts: cabc.Sequence[str],
        dest: str | None,
        action: str | None = None,
        nargs: int = 1,
        const: t.Any | None = None,
    ):
        self._short_opts: list[str] = []
        self._long_opts: list[str] = []
        self.prefixes: set[str] = set()

        for opt in opts:
            prefix, name = _split_opt(opt)
            if not prefix:
                raise ValueError(f"Invalid start character for option ({opt})")
            self.prefixes.add(prefix[0])
            if len(prefix) == 1 and len(name) == 1:
                self._short_opts.append(opt)
            else:
                self._long_opts.append(opt)
                self.prefixes.add(prefix)

        self.dest = dest
        self.action = action or "store"
        self.nargs = nargs
        self.const = const
        self.obj = obj

    @property
    def takes_value(self) -> bool:
        return self.action in ("store", "append")

    def process(self, value: t.Any, state: _ParsingState) -> None:
        dest = self.dest
        if self.action == "store":
            state.opts[dest] = value  # type: ignore[index]
        elif self.action == "store_const":
            state.opts[dest] = self.const  # type: ignore[index]
        elif self.action == "append":
            state.opts.setdefault(dest, []).append(value)  # type: ignore[arg-type]
        elif self.action == "append_const":
            state.opts.setdefault(dest, []).append(self.const)  # type: ignore[arg-type]
        elif self.action == "count":
            state.opts[dest] = state.opts.get(dest, 0) + 1  # type: ignore
        else:
            raise ValueError(f"unknown action '{self.action}'")
        state.order.append(self.obj)


class _Argument:
    def __init__(self, obj: CoreArgument, dest: str | None, nargs: int = 1):
        self.dest = dest
        self.nargs = nargs
        self.obj = obj

    def process(self, value: t.Any, state: _ParsingState) -> None:
        if self.nargs > 1:
            assert value is not None
            missing = sum(1 for v in value if v is None)
            if missing == len(value):
                value = None
            elif missing:
                raise BadArgumentUsage(
                    _("Argument {name!r} takes {nargs} values.").format(
                        name=self.dest, nargs=self.nargs
                    )
                )

        # Nothing given on the command line: leave the value unset so that
        # the environment or a default can provide it.
        given = value is not None and not (self.nargs == -1 and value == ())
        if given:
            state.opts[self.dest] = value  # type: ignore[index]
        state.order.append(self.obj)


class _ParsingState:
    def __init__(self, rargs: list[str]) -> None:
        self.opts: dict[str, t.Any] = {}
        self.largs: list[str] = []
        self.rargs = rargs
        self.order: list[CoreParameter] = []


class _OptionParser:
    """Parses tokens according to the registered options and arguments.

    :param ctx: the context whose settings (interspersed arguments, unknown
        options, token normalization) apply.
    """

    def __init__(self, ctx: Context | None = None) -> None:
        self.ctx = ctx
        self.allow_interspersed_args: bool = True
        self.ignore_unknown_options: bool = False
        if ctx is not None:
            self.allow_interspersed_args = ctx.allow_interspersed_args
            self.ignore_unknown_options = ctx.ignore_unknown_options

        self._short_opt: dict[str, _Option] = {}
        self._long_opt: dict[str, _Option] = {}
        self._opt_prefixes = {"-", "--"}
        self._args: list[_Argument] = []

    def add_option(
        self,
        obj: CoreOption,
        opts: cabc.Sequence[str],
        dest: str | None,
        action: str | None = None,
        nargs: int = 1,
        const: t.Any | None = None,
    ) -> None:
        opts = [_normalize_opt(opt, self.ctx) for opt in opts]
        option = _Option(obj, opts, dest, action=action, nargs=nargs, const=const)
        self._opt_prefixes.update(option.prefixes)
        for opt in option._short_opts:
            self._short_opt[opt] = option
        for opt in option._long_opts:
            self._long_opt[opt] = option

    def add_argument(self, obj: CoreArgument, dest: str | None, nargs: int = 1) -> None:
        self._args.append(_Argument(obj, dest=dest, nargs=nargs))

    def parse_args(
        self, args: list[str]
    ) -> tuple[dict[str, t.Any], list[str], list[CoreParameter]]:
        """Parse ``args``. Returns the collected values by destination, the
        unused arguments, and the parameters in the order they were seen.
        """
        state = _ParsingState(args)
        try:
            self._scan_options(state)
            self._assign_arguments(state)
        except UsageError:
            if self.ctx is None or not self.ctx.resilient_parsing:
                raise
        return state.opts, state.largs, state.order

    def _assign_arguments(self, state: _ParsingState) -> None:
        values, rest = _unpack_args(
            state.largs + state.rargs, [a.nargs for a in self._args]
        )
        for arg, value in zip(self._args, values):
            arg.process(value, state)
        state.largs = rest
        state.rargs = []

    def _scan_options(self, state: _ParsingState) -> None:
        while state.rargs:
            token = state.rargs.pop(0)
            if token == "--":
                return
            if token[:1] in self._opt_prefixes and len(token) > 1:
                self._handle_option_token(token, state)
            elif self.allow_interspersed_args:
                state.largs.append(token)
            else:
                # Positional argument found and interspersing is off: the
                # rest belongs to someone else (a subcommand).
                state.rargs.insert(0, token)
                return

    def _handle_option_token(self, token: str, state: _ParsingState) -> None:
        explicit: str | None = None
        name = token
        if "=" in token:
            name, explicit = token.split("=", 1)
        name = _normalize_opt(name, self.ctx)

        try:
            self._match_long(name, explicit, state)
        except NoSuchOption:
            if token[:2] not in self._opt_prefixes:
                # Could be a cluster of short options like ``-abc``.
                self._match_short(token, state)
                return
            if not self.ignore_unknown_options:
                raise
            state.largs.append(token)

    def _match_long(self, opt: str, explicit: str | None, state: _ParsingState) -> None:
        option = self._long_opt.get(opt)
        if option is None:
            from difflib import get_close_matches

            raise NoSuchOption(
                opt, possibilities=get_close_matches(opt, self._long_opt), ctx=self.ctx
            )

        if option.takes_value:
            if explicit is not None:
                state.rargs.insert(0, explicit)
            value = self._take_value(opt, option, state)
        elif explicit is not None:
            raise BadOptionUsage(
                opt, _("Option {name!r} does not take a value.").format(name=opt)
            )
        else:
            value = None
        option.process(value, state)

    def _match_short(self, token: str, state: _ParsingState) -> None:
        prefix = token[0]
        unknown: list[str] = []

        for pos, char in enumerate(token[1:], start=2):
            opt = _normalize_opt(f"{prefix}{char}", self.ctx)
            option = self._short_opt.get(opt)
            if option is None:
                if self.ignore_unknown_options:
                    unknown.append(char)
                    continue
                raise NoSuchOption(opt, ctx=self.ctx)

            if option.takes_value:
                # The rest of the token is the value, as in ``-ovalue``.
                rest = token[pos:]
                if rest:
                    state.rargs.insert(0, rest)
                option.process(self._take_value(opt, option, state), state)
                break
            option.process(None, state)

        if unknown and self.ignore_unknown_options:
            state.largs.append(f"{prefix}{''.join(unknown)}")

    def _take_value(self, name: str, option: _Option, state: _ParsingState) -> t.Any:
        nargs = option.nargs
        optional = getattr(option.obj, "_flag_needs_value", False)

        if len(state.rargs) < nargs:
            if optional:
                return _flag_needs_value
            raise BadOptionUsage(
                name,
                ngettext(
                    "Option {name!r} requires an argument.",
                    "Option {name!r} requires {nargs} arguments.",
                    nargs,
                ).format(name=name, nargs=nargs),
            )

        if nargs == 1:
            upcoming = state.rargs[0]
            if (
                optional
                and isinstance(upcoming, str)
                and upcoming[:1] in self._opt_prefixes
                and len(upcoming) > 1
            ):
                # Next token is another option, so this one has no value.
                return _flag_needs_value
            return state.rargs.pop(0)

        value = tuple(state.rargs[:nargs])
        del state.rargs[:nargs]
        return value


_DEPRECATED_NAMES = {
    "OptionParser": "_OptionParser",
    "Argument": "_Argument",
    "Option": "_Option",
    "ParsingState": "_ParsingState",
    "split_opt": "_split_opt",
    "normalize_opt": "_normalize_opt",
}


def __getattr__(name: str) -> object:
    import warnings

    if name in _DEPRECATED_NAMES:
        warnings.warn(
            f"'parser.{name}' is deprecated and will be removed in Click 9.0."
            " The old parser is available in 'optparse'.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[_DEPRECATED_NAMES[name]]

    if name == "split_arg_string":
        from .shell_completion import split_arg_string

        warnings.warn(
            "Importing 'parser.split_arg_string' is deprecated, it will only be"
            " available in 'shell_completion' in Click 9.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return split_arg_string

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
