"""Decorators that build commands and parameters from functions."""

from __future__ import annotations

import inspect
import typing as t
from functools import update_wrapper
from gettext import gettext as _

from .core import Argument
from .core import Command
from .core import Context
from .core import Group
from .core import Option
from .core import Parameter
from .globals import get_current_context
from .utils import echo

if t.TYPE_CHECKING:
    import typing_extensions as te

    P = te.ParamSpec("P")

R = t.TypeVar("R")
T = t.TypeVar("T")
_AnyCallable = t.Callable[..., t.Any]
FC = t.TypeVar("FC", bound="_AnyCallable | Command")


def pass_context(f: t.Callable[te.Concatenate[Context, P], R]) -> t.Callable[P, R]:
    """Pass the current context as the first argument."""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return f(get_current_context(), *args, **kwargs)

    return update_wrapper(wrapper, f)


def pass_obj(f: t.Callable[te.Concatenate[T, P], R]) -> t.Callable[P, R]:
    """Pass the context's :attr:`~Context.obj` as the first argument."""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return f(get_current_context().obj, *args, **kwargs)

    return update_wrapper(wrapper, f)


def make_pass_decorator(
    object_type: type[T], ensure: bool = False
) -> t.Callable[[t.Callable[te.Concatenate[T, P], R]], t.Callable[P, R]]:
    """Build a decorator that passes the nearest context object of
    ``object_type`` as first argument.

    :param ensure: create the object if none is found, instead of failing.
    """

    def decorator(f: t.Callable[te.Concatenate[T, P], R]) -> t.Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_current_context()
            if ensure:
                obj = ctx.ensure_object(object_type)
            else:
                obj = ctx.find_object(object_type)
            if obj is None:
                raise RuntimeError(
                    "Managed to invoke callback without a context"
                    f" object of type {object_type.__name__!r}"
                    " existing."
                )
            return ctx.invoke(f, obj, *args, **kwargs)

        return update_wrapper(wrapper, f)

    return decorator


def pass_meta_key(
    key: str, *, doc_description: str | None = None
) -> t.Callable[[t.Callable[te.Concatenate[T, P], R]], t.Callable[P, R]]:
    """Build a decorator that passes ``ctx.meta[key]`` as first argument.

    :param doc_description: how the value is described in the generated
        decorator's docstring.
    """

    def decorator(f: t.Callable[te.Concatenate[T, P], R]) -> t.Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_current_context()
            return ctx.invoke(f, ctx.meta[key], *args, **kwargs)

        return update_wrapper(wrapper, f)

    if doc_description is None:
        doc_description = f"the {key!r} key from :attr:`click.Context.meta`"
    decorator.__doc__ = (
        f"Decorator that passes {doc_description} as the first argument"
        " to the decorated function."
    )
    return decorator


CmdType = t.TypeVar("CmdType", bound=Command)
GrpType = t.TypeVar("GrpType", bound=Group)

_SUFFIXES = {"command", "cmd", "group", "grp"}


def _command_name(func: t.Callable[..., t.Any]) -> str:
    """Derive a command name from a function name: lower case, dashes for
    underscores, and a trailing ``_command``/``_group`` (and the short
    forms) removed.
    """
    name = func.__name__.lower().replace("_", "-")
    head, sep, tail = name.rpartition("-")
    if sep and tail in _SUFFIXES:
        name = head
    return name


@t.overload
def command(name: _AnyCallable) -> Command: ...


@t.overload
def command(
    name: str | None,
    cls: type[CmdType],
    **attrs: t.Any,
) -> t.Callable[[_AnyCallable], CmdType]: ...


@t.overload
def command(
    name: None = None,
    *,
    cls: type[CmdType],
    **attrs: t.Any,
) -> t.Callable[[_AnyCallable], CmdType]: ...


@t.overload
def command(
    name: str | None = ..., cls: None = None, **attrs: t.Any
) -> t.Callable[[_AnyCallable], Command]: ...


def command(
    name: str | _AnyCallable | None = None,
    cls: type[CmdType] | None = None,
    **attrs: t.Any,
) -> Command | t.Callable[[_AnyCallable], Command | CmdType]:
    """Turn a function into a :class:`Command`.

    The name defaults to the function name. The docstring becomes the help
    text unless ``help`` is given. Parameters added with :func:`option`
    and :func:`argument` are attached in the order they appear in the
    source.

    Can be used with or without parentheses.

    :param name: the command name.
    :param cls: the command class to create.
    :param attrs: passed to the command class.
    """
    func: t.Callable[..., t.Any] | None = None
    if callable(name):
        func = name
        name = None
        assert cls is None, "Use 'command(cls=cls)(callable)' to specify a class."
        assert not attrs, "Use 'command(**kwargs)(callable)' to provide arguments."

    command_cls: type[Command] = Command if cls is None else cls

    def decorator(f: _AnyCallable) -> Command:
        if isinstance(f, Command):
            raise TypeError("Attempted to convert a callback into a command twice.")

        explicit = attrs.pop("params", None)
        params: list[Parameter] = explicit if explicit is not None else []

        collected = getattr(f, "__click_params__", None)
        if collected is not None:
            del f.__click_params__  # type: ignore[attr-defined]
            # Decorators run bottom-up, so they were collected in reverse.
            params.extend(reversed(collected))

        if attrs.get("help") is None:
            attrs["help"] = f.__doc__

        cmd = command_cls(
            name=name or _command_name(f),
            callback=f,
            params=params,
            **attrs,
        )
        cmd.__doc__ = f.__doc__
        return cmd

    if func is not None:
        return decorator(func)
    return decorator


@t.overload
def group(name: _AnyCallable) -> Group: ...


@t.overload
def group(
    name: str | None,
    cls: type[GrpType],
    **attrs: t.Any,
) -> t.Callable[[_AnyCallable], GrpType]: ...


@t.overload
def group(
    name: None = None,
    *,
    cls: type[GrpType],
    **attrs: t.Any,
) -> t.Callable[[_AnyCallable], GrpType]: ...


@t.overload
def group(
    name: str | None = ..., cls: None = None, **attrs: t.Any
) -> t.Callable[[_AnyCallable], Group]: ...


def group(
    name: str | _AnyCallable | None = None,
    cls: type[GrpType] | None = None,
    **attrs: t.Any,
) -> Group | t.Callable[[_AnyCallable], Group | GrpType]:
    """Like :func:`command`, but creates a :class:`Group`."""
    if cls is None:
        cls = t.cast("type[GrpType]", Group)
    if callable(name):
        return command(cls=cls, **attrs)(name)
    return command(name, cls, **attrs)


def _param_memo(f: t.Callable[..., t.Any], param: Parameter) -> None:
    if isinstance(f, Command):
        f.params.append(param)
        return
    if not hasattr(f, "__click_params__"):
        f.__click_params__ = []  # type: ignore[attr-defined]
    f.__click_params__.append(param)  # type: ignore[attr-defined]


def argument(
    *param_decls: str, cls: type[Argument] | None = None, **attrs: t.Any
) -> t.Callable[[FC], FC]:
    """Add an :class:`Argument` to the command.

    :param cls: the argument class to create.
    :param attrs: passed to the argument class.
    """
    arg_cls = Argument if cls is None else cls

    def decorator(f: FC) -> FC:
        _param_memo(f, arg_cls(param_decls, **attrs))
        return f

    return decorator


def option(
    *param_decls: str, cls: type[Option] | None = None, **attrs: t.Any
) -> t.Callable[[FC], FC]:
    """Add an :class:`Option` to the command.

    :param cls: the option class to create.
    :param attrs: passed to the option class.
    """
    opt_cls = Option if cls is None else cls

    def decorator(f: FC) -> FC:
        _param_memo(f, opt_cls(param_decls, **attrs))
        return f

    return decorator


def confirmation_option(*param_decls: str, **kwargs: t.Any) -> t.Callable[[FC], FC]:
    """Add a ``--yes`` flag. Without it the user is asked to confirm and
    the command aborts if they decline.
    """

    def callback(ctx: Context, param: Parameter, value: bool) -> None:
        if not value:
            ctx.abort()

    if not param_decls:
        param_decls = ("--yes",)

    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("callback", callback)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("prompt", "Do you want to continue?")
    kwargs.setdefault("help", "Confirm the action without prompting.")
    return option(*param_decls, **kwargs)


def password_option(*param_decls: str, **kwargs: t.Any) -> t.Callable[[FC], FC]:
    """Add a ``--password`` option that is prompted for with hidden input
    and confirmation.
    """
    if not param_decls:
        param_decls = ("--password",)

    kwargs.setdefault("prompt", True)
    kwargs.setdefault("confirmation_prompt", True)
    kwargs.setdefault("hide_input", True)
    return option(*param_decls, **kwargs)


def version_option(
    version: str | None = None,
    *param_decls: str,
    package_name: str | None = None,
    prog_name: str | None = None,
    message: str | None = None,
    **kwargs: t.Any,
) -> t.Callable[[FC], FC]:
    """Add a ``--version`` flag that prints the version and exits.

    Without ``version``, it is looked up from the installed metadata of
    ``package_name``, which defaults to the top-level package of the
    module calling this decorator.

    :param message: format string with ``%(prog)s``, ``%(package)s`` and
        ``%(version)s``.
    """
    if message is None:
        message = _("%(prog)s, version %(version)s")

    if version is None and package_name is None:
        frame = inspect.currentframe()
        caller_globals = frame.f_back.f_globals if frame and frame.f_back else None
        del frame
        if caller_globals is not None:
            package_name = caller_globals.get("__name__")
            if package_name == "__main__":
                package_name = caller_globals.get("__package__")
            if package_name:
                package_name = package_name.partition(".")[0]

    def callback(ctx: Context, param: Parameter, value: bool) -> None:
        if not value or ctx.resilient_parsing:
            return

        nonlocal prog_name, version
        if prog_name is None:
            prog_name = ctx.find_root().info_name

        if version is None and package_name is not None:
            import importlib.metadata

            try:
                version = importlib.metadata.version(package_name)
            except importlib.metadata.PackageNotFoundError:
                raise RuntimeError(
                    f"{package_name!r} is not installed. Try passing"
                    " 'package_name' instead."
                ) from None

        if version is None:
            raise RuntimeError(
                f"Could not determine the version for {package_name!r} automatically."
            )

        echo(
            message % {"prog": prog_name, "package": package_name, "version": version},
            color=ctx.color,
        )
        ctx.exit()

    if not param_decls:
        param_decls = ("--version",)

    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("is_eager", True)
    kwargs.setdefault("help", _("Show the version and exit."))
    kwargs["callback"] = callback
    return option(*param_decls, **kwargs)


def help_option(*param_decls: str, **kwargs: t.Any) -> t.Callable[[FC], FC]:
    """Add a ``--help`` flag that prints the help page and exits. Commands
    have one by default; this is for customizing it.
    """

    def show_help(ctx: Context, param: Parameter, value: bool) -> None:
        if not value or ctx.resilient_parsing:
            return
        echo(ctx.get_help(), color=ctx.color)
        ctx.exit()

    if not param_decls:
        param_decls = ("--help",)

    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("is_eager", True)
    kwargs.setdefault("help", _("Show this message and exit."))
    kwargs.setdefault("callback", show_help)
    return option(*param_decls, **kwargs)
