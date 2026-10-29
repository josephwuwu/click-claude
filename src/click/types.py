"""Parameter types: convert and validate the raw strings from the command
line into Python values.
"""

from __future__ import annotations

import collections.abc as cabc
import enum
import os
import stat
import sys
import typing as t
import uuid
from datetime import datetime
from gettext import gettext as _
from gettext import ngettext

from ._compat import get_filesystem_encoding
from ._compat import open_stream
from .exceptions import BadParameter
from .utils import _LazyFile
from .utils import _safecall
from .utils import format_filename

if t.TYPE_CHECKING:
    from .core import Context
    from .core import Parameter
    from .shell_completion import CompletionItem

ParamTypeValue = t.TypeVar("ParamTypeValue")

#: The converted value of a type, and the value it accepts. The input
#: defaults to ``Any`` so that ``ParamType[int]`` is enough.
if sys.version_info >= (3, 13):
    ParamTypeInput = t.TypeVar("ParamTypeInput", default=t.Any)  # type: ignore[call-arg]
else:  # pragma: no cover
    ParamTypeInput = t.TypeVar("ParamTypeInput")

PathValue = t.TypeVar("PathValue")


class ParamType(t.Generic[ParamTypeValue, ParamTypeInput]):
    """Base class for parameter types.

    Subclasses set :attr:`name` and implement :meth:`convert`. Conversion
    must accept values that were already converted, because defaults and
    values passed from Python go through it too.
    """

    is_composite: t.ClassVar[bool] = False
    arity: t.ClassVar[int] = 1

    if sys.version_info < (3, 13):  # pragma: no cover

        def __class_getitem__(cls, params: t.Any) -> t.Any:
            # Backfill the input parameter's default for older Pythons.
            if cls is ParamType and not isinstance(params, tuple):
                params = (params, t.Any)
            return super().__class_getitem__(params)  # type: ignore[misc]

    #: Shown in help and error messages.
    name: str

    #: Separator used when a value from an environment variable must be
    #: split into several values. ``None`` splits on any whitespace.
    envvar_list_splitter: t.ClassVar[str | None] = None

    def to_info_dict(self) -> dict[str, t.Any]:
        """Describe the type for documentation tools."""
        kind = type(self).__name__.partition("ParamType")[0]
        kind = kind.partition("ParameterType")[0]
        return {"param_type": kind, "name": getattr(self, "name", kind)}

    def __call__(
        self,
        value: t.Any,
        param: Parameter | None = None,
        ctx: Context | None = None,
    ) -> t.Any:
        if value is not None:
            return self.convert(value, param, ctx)
        return None

    def get_metavar(self, param: Parameter, ctx: Context) -> str | None:
        """Placeholder shown for the value in help, if the type wants a
        custom one.
        """
        return None

    def get_missing_message(self, param: Parameter, ctx: Context | None) -> str | None:
        """Extra text added to the error when the value is missing."""
        return None

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        """Return ``value`` converted to this type, or call :meth:`fail`."""
        return value

    def split_envvar_value(self, rv: str) -> cabc.Sequence[str]:
        """Split an environment variable value into several values."""
        return (rv or "").split(self.envvar_list_splitter)

    def fail(
        self,
        message: str,
        param: Parameter | None = None,
        ctx: Context | None = None,
    ) -> t.NoReturn:
        """Raise a :exc:`BadParameter` for the given parameter."""
        raise BadParameter(message, ctx=ctx, param=param)

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        """Completions for a partially typed value."""
        return []


class CompositeParamType(ParamType):
    """A type that consumes several command line values at once."""

    is_composite = True

    @property
    def arity(self) -> int:  # type: ignore[override]
        raise NotImplementedError()


class FuncParamType(ParamType):
    """Wraps a plain callable such as ``int`` or a user function."""

    def __init__(self, func: t.Callable[[t.Any], t.Any]) -> None:
        self.name: str = func.__name__
        self.func = func

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info["func"] = self.func
        return info

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        try:
            return self.func(value)
        except ValueError as e:
            # Prefer the function's own explanation; fall back to the value.
            text = str(e)
            if not text:
                try:
                    text = str(value)
                except UnicodeError:
                    text = value.decode("utf-8", "replace")
            self.fail(text, param, ctx)


class UnprocessedParamType(ParamType):
    name = "text"

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        return value

    def __repr__(self) -> str:
        return "UNPROCESSED"


class StringParamType(ParamType):
    name = "text"

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        if not isinstance(value, bytes):
            return str(value)
        candidates = [getattr(sys.stdin, "encoding", None), get_filesystem_encoding()]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return value.decode(encoding)
            except UnicodeError:
                continue
        return value.decode("utf-8", "replace")

    def __repr__(self) -> str:
        return "STRING"


class Choice(ParamType[ParamTypeValue]):
    """Accept only one of a fixed set of values.

    Choices may be strings or other objects such as enum members; the
    value from the command line is compared against the choice's text (an
    enum member's name) and the original choice object is returned.

    :param case_sensitive: compare without regard to case when false.
    """

    name = "choice"

    def __init__(
        self, choices: cabc.Iterable[ParamTypeValue], case_sensitive: bool = True
    ) -> None:
        self.choices: cabc.Sequence[ParamTypeValue] = tuple(choices)
        self.case_sensitive = case_sensitive

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info["choices"] = self.choices
        info["case_sensitive"] = self.case_sensitive
        return info

    @staticmethod
    def _label(choice: t.Any) -> str:
        if isinstance(choice, enum.Enum):
            return choice.name
        return str(choice)

    def normalize_choice(self, choice: ParamTypeValue, ctx: Context | None) -> str:
        """Text used when comparing ``choice`` with user input."""
        text = self._label(choice)
        if ctx is not None and ctx.token_normalize_func is not None:
            text = ctx.token_normalize_func(text)
        if not self.case_sensitive:
            text = text.casefold()
        return text

    def _normalized_mapping(
        self, ctx: Context | None = None
    ) -> cabc.Mapping[ParamTypeValue, str]:
        return {choice: self.normalize_choice(choice, ctx) for choice in self.choices}

    def get_metavar(self, param: Parameter, ctx: Context) -> str | None:
        if param.param_type_name == "option" and not getattr(
            param, "show_choices", True
        ):
            names = [convert_type(type(c)).name.upper() for c in self.choices]
            body = "|".join(dict.fromkeys(names))
        else:
            body = "|".join(self._normalized_mapping(ctx).values())

        if param.param_type_name == "argument" and param.required:
            return f"{{{body}}}"
        return f"[{body}]"

    def get_missing_message(self, param: Parameter, ctx: Context | None) -> str:
        listing = ",\n\t".join(self._normalized_mapping(ctx).values())
        return _("Choose from:\n\t{choices}").format(choices=listing)

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> ParamTypeValue:
        wanted = self.normalize_choice(value, ctx)
        for choice, normalized in self._normalized_mapping(ctx).items():
            if normalized == wanted:
                return choice
        self.fail(self.get_invalid_choice_message(value, ctx), param, ctx)

    def get_invalid_choice_message(self, value: t.Any, ctx: Context | None) -> str:
        listing = ", ".join(repr(c) for c in self._normalized_mapping(ctx).values())
        return ngettext(
            "{value!r} is not {choice}.",
            "{value!r} is not one of {choices}.",
            len(self.choices),
        ).format(value=value, choice=listing, choices=listing)

    def __repr__(self) -> str:
        return f"Choice({list(self.choices)})"

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        from .shell_completion import CompletionItem

        labels = list(self._normalized_mapping(ctx).values())
        if not self.case_sensitive:
            incomplete = incomplete.casefold()
        return [CompletionItem(s) for s in labels if s.startswith(incomplete)]


class DateTime(ParamType):
    """Parse a date or time using the first of ``formats`` that matches.
    The default formats accept ``YYYY-MM-DD`` with an optional time.
    """

    name = "datetime"

    def __init__(self, formats: cabc.Sequence[str] | None = None):
        self.formats: cabc.Sequence[str] = formats or [
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info["formats"] = self.formats
        return info

    def get_metavar(self, param: Parameter, ctx: Context) -> str | None:
        return f"[{'|'.join(self.formats)}]"

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        if isinstance(value, datetime):
            return value
        for fmt in self.formats:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                pass
        listing = ", ".join(repr(f) for f in self.formats)
        self.fail(
            ngettext(
                "{value!r} does not match the format {format}.",
                "{value!r} does not match the formats {formats}.",
                len(self.formats),
            ).format(value=value, format=listing, formats=listing),
            param,
            ctx,
        )

    def __repr__(self) -> str:
        return "DateTime"


class _NumberParamTypeBase(ParamType):
    _number_class: t.ClassVar[type[t.Any]]

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        try:
            return self._number_class(value)
        except ValueError:
            self.fail(
                _("{value!r} is not a valid {number_type}.").format(
                    value=value, number_type=self.name
                ),
                param,
                ctx,
            )


class _NumberRangeBase(_NumberParamTypeBase):
    def __init__(
        self,
        min: float | None = None,
        max: float | None = None,
        min_open: bool = False,
        max_open: bool = False,
        clamp: bool = False,
    ) -> None:
        self.min = min
        self.max = max
        self.min_open = min_open
        self.max_open = max_open
        self.clamp = clamp

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info.update(
            min=self.min,
            max=self.max,
            min_open=self.min_open,
            max_open=self.max_open,
            clamp=self.clamp,
        )
        return info

    def _below_min(self, value: t.Any) -> bool:
        if self.min is None:
            return False
        return value <= self.min if self.min_open else value < self.min

    def _above_max(self, value: t.Any) -> bool:
        if self.max is None:
            return False
        return value >= self.max if self.max_open else value > self.max

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        number = super().convert(value, param, ctx)
        low = self._below_min(number)
        high = self._above_max(number)

        if self.clamp:
            if low:
                return self._clamp(self.min, 1, self.min_open)  # type: ignore[arg-type]
            if high:
                return self._clamp(self.max, -1, self.max_open)  # type: ignore[arg-type]

        if low or high:
            self.fail(
                _("{value} is not in the range {range}.").format(
                    value=number, range=self._describe_range()
                ),
                param,
                ctx,
            )
        return number

    def _clamp(self, bound: float, dir: t.Literal[1, -1], open: bool) -> float:
        """Closest valid value to ``bound``; ``dir`` points into the range."""
        raise NotImplementedError

    def _describe_range(self) -> str:
        """Render the range like ``0<=x<10``."""
        if self.min is None:
            return f"x{'<' if self.max_open else '<='}{self.max}"
        if self.max is None:
            return f"x{'>' if self.min_open else '>='}{self.min}"
        low = "<" if self.min_open else "<="
        high = "<" if self.max_open else "<="
        return f"{self.min}{low}x{high}{self.max}"

    def __repr__(self) -> str:
        suffix = " clamped" if self.clamp else ""
        return f"<{type(self).__name__} {self._describe_range()}{suffix}>"


class IntParamType(_NumberParamTypeBase):
    name = "integer"
    _number_class = int

    def __repr__(self) -> str:
        return "INT"


class IntRange(_NumberRangeBase, IntParamType):
    """An integer limited to a range. Out of range values are an error, or
    are moved to the nearest bound when ``clamp`` is set. Open bounds
    exclude the bound itself.
    """

    name = "integer range"

    def _clamp(self, bound: int, dir: t.Literal[1, -1], open: bool) -> int:  # type: ignore[override]
        return bound + dir if open else bound


class FloatParamType(_NumberParamTypeBase):
    name = "float"
    _number_class = float

    def __repr__(self) -> str:
        return "FLOAT"


class FloatRange(_NumberRangeBase, FloatParamType):
    """A float limited to a range. Clamping cannot be combined with open
    bounds, since there is no closest float inside an open range.
    """

    name = "float range"

    def __init__(
        self,
        min: float | None = None,
        max: float | None = None,
        min_open: bool = False,
        max_open: bool = False,
        clamp: bool = False,
    ) -> None:
        super().__init__(min=min, max=max, min_open=min_open, max_open=max_open, clamp=clamp)
        if clamp and (min_open or max_open):
            raise TypeError("Clamping is not supported for open bounds.")

    def _clamp(self, bound: float, dir: t.Literal[1, -1], open: bool) -> float:
        if open:
            raise RuntimeError("Clamping is not supported for open bounds.")
        return bound


_TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_WORDS = frozenset({"0", "false", "f", "no", "n", "off"})


class BoolParamType(ParamType):
    name = "boolean"

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        if value is True or value is False:
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        word = str(value).strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        self.fail(
            _("{value!r} is not a valid boolean.").format(value=value), param, ctx
        )

    def __repr__(self) -> str:
        return "BOOL"


class UUIDParameterType(ParamType):
    name = "uuid"

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        if isinstance(value, uuid.UUID):
            return value
        text = value.strip()
        try:
            return uuid.UUID(text)
        except ValueError:
            self.fail(
                _("{value!r} is not a valid UUID.").format(value=text), param, ctx
            )

    def __repr__(self) -> str:
        return "UUID"


class File(ParamType):
    """Open the given path as a file. ``"-"`` means stdin or stdout
    depending on the mode.

    Files are closed when the context is torn down. Files for writing are
    opened lazily by default, so that a file is not created if the command
    fails before writing anything.

    :param lazy: force lazy or immediate opening.
    :param atomic: write into a temporary file that replaces the target
        when closed.
    """

    name = "filename"
    envvar_list_splitter: t.ClassVar[str] = os.path.pathsep

    def __init__(
        self,
        mode: str = "r",
        encoding: str | None = None,
        errors: str | None = "strict",
        lazy: bool | None = None,
        atomic: bool = False,
    ) -> None:
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.lazy = lazy
        self.atomic = atomic

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info.update(mode=self.mode, encoding=self.encoding)
        return info

    def resolve_lazy_flag(self, value: str | os.PathLike[str]) -> bool:
        if self.lazy is not None:
            return self.lazy
        if os.fspath(value) == "-":
            return False
        return "w" in self.mode

    def convert(
        self,
        value: str | os.PathLike[str] | t.IO[t.Any],
        param: Parameter | None,
        ctx: Context | None,
    ) -> t.IO[t.Any]:
        if hasattr(value, "read") or hasattr(value, "write"):
            return t.cast("t.IO[t.Any]", value)

        path = t.cast("str | os.PathLike[str]", value)
        try:
            if self.resolve_lazy_flag(path):
                lazy = _LazyFile(path, self.mode, self.encoding, self.errors, atomic=self.atomic)
                if ctx is not None:
                    ctx.call_on_close(lazy.close_intelligently)
                return t.cast("t.IO[t.Any]", lazy)

            f, should_close = open_stream(
                path, self.mode, self.encoding, self.errors, atomic=self.atomic
            )
            if ctx is not None:
                # Standard streams are only flushed, never closed.
                ctx.call_on_close(_safecall(f.close if should_close else f.flush))
            return f
        except OSError as e:
            self.fail(f"'{format_filename(path)}': {e.strerror}", param, ctx)

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        from .shell_completion import CompletionItem

        return [CompletionItem(incomplete, type="file")]


class Path(ParamType[PathValue]):
    """A filesystem path, optionally checked for existence, kind and
    permissions. The value is not opened.

    :param exists: the path must exist.
    :param file_okay: regular files are allowed.
    :param dir_okay: directories are allowed.
    :param readable: must be readable (only checked if it exists).
    :param writable: must be writable (only checked if it exists).
    :param executable: must be executable (only checked if it exists).
    :param resolve_path: make absolute and resolve symlinks.
    :param allow_dash: accept ``"-"`` for the standard streams.
    :param path_type: convert the result to this type, such as
        :class:`pathlib.Path`.
    """

    envvar_list_splitter: t.ClassVar[str] = os.path.pathsep

    def __init__(
        self,
        exists: bool = False,
        file_okay: bool = True,
        dir_okay: bool = True,
        writable: bool = False,
        readable: bool = True,
        resolve_path: bool = False,
        allow_dash: bool = False,
        path_type: type[t.Any] | None = None,
        executable: bool = False,
    ):
        self.exists = exists
        self.file_okay = file_okay
        self.dir_okay = dir_okay
        self.readable = readable
        self.writable = writable
        self.executable = executable
        self.resolve_path = resolve_path
        self.allow_dash = allow_dash
        self.type = path_type

        if file_okay and not dir_okay:
            self.name: str = _("file")
        elif dir_okay and not file_okay:
            self.name = _("directory")
        else:
            self.name = _("path")

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info.update(
            exists=self.exists,
            file_okay=self.file_okay,
            dir_okay=self.dir_okay,
            writable=self.writable,
            readable=self.readable,
            allow_dash=self.allow_dash,
        )
        return info

    def coerce_path_result(
        self, value: str | os.PathLike[str]
    ) -> str | bytes | os.PathLike[str]:
        if self.type is None or isinstance(value, self.type):
            return value
        if self.type is str:
            return os.fsdecode(value)
        if self.type is bytes:
            return os.fsencode(value)
        return t.cast("os.PathLike[str]", self.type(value))

    def convert(
        self,
        value: str | os.PathLike[str],
        param: Parameter | None,
        ctx: Context | None,
    ) -> str | bytes | os.PathLike[str]:
        path = value
        # Compare like with like, ``b"-" == "-"`` warns under ``python -b``.
        dash: str | bytes = b"-" if isinstance(path, bytes) else "-"
        if self.file_okay and self.allow_dash and path == dash:
            return self.coerce_path_result(path)

        if self.resolve_path:
            path = os.path.realpath(path)

        shown = format_filename(value)
        kind = self.name.title()

        try:
            info = os.stat(path)
        except OSError:
            if not self.exists:
                return self.coerce_path_result(path)
            self.fail(
                _("{name} {filename!r} does not exist.").format(name=kind, filename=shown),
                param,
                ctx,
            )

        if not self.file_okay and stat.S_ISREG(info.st_mode):
            self.fail(
                _("{name} {filename!r} is a file.").format(name=kind, filename=shown),
                param,
                ctx,
            )
        if not self.dir_okay and stat.S_ISDIR(info.st_mode):
            self.fail(
                _("{name} {filename!r} is a directory.").format(name=kind, filename=shown),
                param,
                ctx,
            )

        checks = (
            (self.readable, os.R_OK, _("{name} {filename!r} is not readable.")),
            (self.writable, os.W_OK, _("{name} {filename!r} is not writable.")),
            (self.executable, os.X_OK, _("{name} {filename!r} is not executable.")),
        )
        for wanted, mode, message in checks:
            if wanted and not os.access(path, mode):
                self.fail(message.format(name=kind, filename=shown), param, ctx)

        return self.coerce_path_result(path)

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        from .shell_completion import CompletionItem

        kind = "dir" if self.dir_okay and not self.file_okay else "file"
        return [CompletionItem(incomplete, type=kind)]


class Tuple(CompositeParamType):
    """A fixed number of values, each with its own type. Used for options
    and arguments with ``nargs`` greater than one.
    """

    def __init__(self, types: cabc.Sequence[type[t.Any] | ParamType]) -> None:
        self.types: cabc.Sequence[ParamType] = [convert_type(ty) for ty in types]

    def to_info_dict(self) -> dict[str, t.Any]:
        info = super().to_info_dict()
        info["types"] = [ty.to_info_dict() for ty in self.types]
        return info

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"<{' '.join(ty.name for ty in self.types)}>"

    @property
    def arity(self) -> int:  # type: ignore[override]
        return len(self.types)

    def convert(
        self, value: t.Any, param: Parameter | None, ctx: Context | None
    ) -> t.Any:
        expected = len(self.types)
        if len(value) != expected:
            self.fail(
                ngettext(
                    "{len_type} values are required, but {len_value} was given.",
                    "{len_type} values are required, but {len_value} were given.",
                    len(value),
                ).format(len_type=expected, len_value=len(value)),
                param=param,
                ctx=ctx,
            )
        return tuple(ty(item, param, ctx) for ty, item in zip(self.types, value))


def _guess_type(default: t.Any) -> t.Any:
    """Infer a type from a default value. Sequences are inspected by their
    first element, since they are the defaults of ``multiple`` or ``nargs``
    parameters.
    """
    if isinstance(default, (tuple, list)):
        if not default:
            return None
        first = default[0]
        if isinstance(first, (tuple, list)):
            return tuple(type(item) for item in first)
        return type(first)
    return type(default)


def convert_type(ty: t.Any | None, default: t.Any | None = None) -> ParamType:
    """Turn a Python type, a tuple of types, a :class:`ParamType` or a
    callable into a :class:`ParamType`. Without ``ty`` the type is guessed
    from ``default``.
    """
    guessed = False
    if ty is None and default is not None:
        ty = _guess_type(default)
        guessed = True

    if isinstance(ty, tuple):
        return Tuple(ty)
    if isinstance(ty, ParamType):
        return ty

    builtin = {str: STRING, int: INT, float: FLOAT, bool: BOOL}
    if ty is None:
        return STRING
    if ty in builtin:
        return builtin[ty]
    if guessed:
        return STRING

    if isinstance(ty, type) and issubclass(ty, ParamType):
        raise AssertionError(
            f"Attempted to use an uninstantiated parameter type ({ty})."
        )

    return FuncParamType(ty)


#: Passes values through without any conversion.
UNPROCESSED = UnprocessedParamType()

#: Text; bytes are decoded.
STRING = StringParamType()

#: Integers.
INT = IntParamType()

#: Floating point numbers.
FLOAT = FloatParamType()

#: Booleans, accepting words like ``yes``, ``off`` or ``1``.
BOOL = BoolParamType()

#: UUIDs.
UUID = UUIDParameterType()


class OptionHelpExtra(t.TypedDict, total=False):
    envvars: tuple[str, ...]
    default: str
    range: str
    required: str
