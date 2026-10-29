"""Functions for interacting with the user in a terminal: prompts,
confirmation, styled output, progress bars, paging and editing.
"""

from __future__ import annotations

import collections.abc as cabc
import inspect
import io
import itertools
import sys
import typing as t
from contextlib import AbstractContextManager
from gettext import gettext as _

from ._compat import isatty
from ._compat import strip_ansi
from .exceptions import Abort
from .exceptions import UsageError
from .globals import resolve_color_default
from .types import Choice
from .types import ParamType
from .types import convert_type
from .utils import LazyFile
from .utils import echo

if t.TYPE_CHECKING:
    from ._termui_impl import ProgressBar

V = t.TypeVar("V")

# Replaced by the test runner to feed input without a terminal.
visible_prompt_func: t.Callable[[str], str] = input


def hidden_prompt_func(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


_ansi_colors = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "reset": 39,
    "bright_black": 90,
    "bright_red": 91,
    "bright_green": 92,
    "bright_yellow": 93,
    "bright_blue": 94,
    "bright_magenta": 95,
    "bright_cyan": 96,
    "bright_white": 97,
}
_ansi_reset_all = "\033[0m"


def _build_prompt(
    text: str,
    suffix: str,
    show_default: bool = False,
    default: t.Any | None = None,
    show_choices: bool = True,
    type: ParamType | None = None,
) -> str:
    parts = text
    if type is not None and show_choices and isinstance(type, Choice):
        parts += f" ({', '.join(map(str, type.choices))})"
    if default is not None and show_default:
        parts = f"{parts} [{_format_default(default)}]"
    return f"{parts}{suffix}"


def _format_default(default: t.Any) -> t.Any:
    if isinstance(default, (io.IOBase, LazyFile)) and hasattr(default, "name"):
        return default.name
    return default


def _read_line(prompt_text: str, hidden: bool, err: bool) -> str:
    reader = hidden_prompt_func if hidden else visible_prompt_func
    try:
        # The prompt goes through echo so it respects ``err``; the input
        # function only gets a single space. That keeps readline from
        # erasing the whole prompt on backspace.
        echo(prompt_text.rstrip(" "), nl=False, err=err)
        return reader(" ")
    except (KeyboardInterrupt, EOFError):
        # getpass does not end the line when interrupted.
        if hidden:
            echo(None, err=err)
        raise Abort() from None


def prompt(
    text: str,
    default: t.Any | None = None,
    hide_input: bool = False,
    confirmation_prompt: bool | str = False,
    type: ParamType | t.Any | None = None,
    value_proc: t.Callable[[str], t.Any] | None = None,
    prompt_suffix: str = ": ",
    show_default: bool = True,
    err: bool = False,
    show_choices: bool = True,
) -> t.Any:
    """Ask the user for a value until a valid one is entered.

    :param text: the question.
    :param default: used when the user just presses enter; without one an
        empty answer asks again.
    :param hide_input: do not show what is typed.
    :param confirmation_prompt: ask a second time and require the same
        answer. A string sets the text of the second question.
    :param type: converts and validates the answer.
    :param value_proc: custom conversion function instead of ``type``.
    :param prompt_suffix: appended to the question.
    :param show_default: show the default in brackets.
    :param err: write the question to stderr.
    :param show_choices: list the choices of a :class:`Choice` type.
    """
    if value_proc is None:
        value_proc = convert_type(type, default)

    question = _build_prompt(text, prompt_suffix, show_default, default, show_choices, type)

    repeat_question: str | None = None
    if confirmation_prompt:
        if confirmation_prompt is True:
            confirmation_prompt = _("Repeat for confirmation")
        repeat_question = _build_prompt(confirmation_prompt, prompt_suffix)

    while True:
        while True:
            value = _read_line(question, hide_input, err)
            if value:
                break
            if default is not None:
                value = default
                break

        try:
            result = value_proc(value)
        except UsageError as e:
            if hide_input:
                echo(_("Error: The value you entered was invalid."), err=err)
            else:
                echo(_("Error: {e.message}").format(e=e), err=err)
            continue

        if repeat_question is None:
            return result

        while True:
            again = _read_line(repeat_question, hide_input, err)
            # Accept an empty repeat only if the first answer was empty too.
            if again or (not value and not again):
                break

        if value == again:
            return result
        echo(_("Error: The two entered values do not match."), err=err)


def confirm(
    text: str,
    default: bool | None = False,
    abort: bool = False,
    prompt_suffix: str = ": ",
    show_default: bool = True,
    err: bool = False,
) -> bool:
    """Ask a yes/no question.

    :param default: answer used for an empty reply. ``None`` requires an
        explicit answer.
    :param abort: raise :exc:`Abort` if the answer is no.
    """
    if default is None:
        hint = "y/n"
    elif default:
        hint = "Y/n"
    else:
        hint = "y/N"
    question = _build_prompt(text, prompt_suffix, show_default, hint)

    while True:
        try:
            echo(question.rstrip(" "), nl=False, err=err)
            answer = visible_prompt_func(" ").lower().strip()
        except (KeyboardInterrupt, EOFError):
            raise Abort() from None

        if answer in ("y", "yes"):
            rv = True
        elif answer in ("n", "no"):
            rv = False
        elif default is not None and answer == "":
            rv = default
        else:
            echo(_("Error: invalid input"), err=err)
            continue
        break

    if abort and not rv:
        raise Abort()
    return rv


def echo_via_pager(
    text_or_generator: cabc.Iterable[str] | t.Callable[[], cabc.Iterable[str]] | str,
    color: bool | None = None,
) -> None:
    """Show text through a pager such as ``less``, if the output is a
    terminal.

    :param text_or_generator: text, an iterable of text, or a function
        returning one.
    :param color: keep styling in the output.
    """
    color = resolve_color_default(color)

    if inspect.isgeneratorfunction(text_or_generator):
        chunks = t.cast("t.Callable[[], cabc.Iterable[str]]", text_or_generator)()
    elif isinstance(text_or_generator, str):
        chunks = [text_or_generator]
    else:
        chunks = iter(t.cast("cabc.Iterable[str]", text_or_generator))

    from ._termui_impl import pager

    pager(itertools.chain((str(c) for c in chunks), "\n"), color)


@t.overload
def progressbar(
    *,
    length: int,
    label: str | None = None,
    hidden: bool = False,
    show_eta: bool = True,
    show_percent: bool | None = None,
    show_pos: bool = False,
    fill_char: str = "#",
    empty_char: str = "-",
    bar_template: str = "%(label)s  [%(bar)s]  %(info)s",
    info_sep: str = "  ",
    width: int = 36,
    file: t.TextIO | None = None,
    color: bool | None = None,
    update_min_steps: int = 1,
) -> ProgressBar[int]: ...


@t.overload
def progressbar(
    iterable: cabc.Iterable[V] | None = None,
    length: int | None = None,
    label: str | None = None,
    hidden: bool = False,
    show_eta: bool = True,
    show_percent: bool | None = None,
    show_pos: bool = False,
    item_show_func: t.Callable[[V | None], str | None] | None = None,
    fill_char: str = "#",
    empty_char: str = "-",
    bar_template: str = "%(label)s  [%(bar)s]  %(info)s",
    info_sep: str = "  ",
    width: int = 36,
    file: t.TextIO | None = None,
    color: bool | None = None,
    update_min_steps: int = 1,
) -> ProgressBar[V]: ...


def progressbar(
    iterable: cabc.Iterable[V] | None = None,
    length: int | None = None,
    label: str | None = None,
    hidden: bool = False,
    show_eta: bool = True,
    show_percent: bool | None = None,
    show_pos: bool = False,
    item_show_func: t.Callable[[V | None], str | None] | None = None,
    fill_char: str = "#",
    empty_char: str = "-",
    bar_template: str = "%(label)s  [%(bar)s]  %(info)s",
    info_sep: str = "  ",
    width: int = 36,
    file: t.TextIO | None = None,
    color: bool | None = None,
    update_min_steps: int = 1,
) -> ProgressBar[V]:
    """Create a progress bar to be used as a context manager. Iterating
    over it yields the items of ``iterable`` and advances the bar;
    alternatively call ``update(n)`` manually.

    When the output is not a terminal only the label is printed once.

    :param iterable: items to iterate over. Without it, ``length`` is
        required and the bar iterates over ``range(length)``.
    :param length: number of items, if ``iterable`` has no length.
    :param label: text shown before the bar.
    :param hidden: render nothing at all.
    :param show_eta: show the estimated remaining time.
    :param show_percent: show the percentage; on by default when the
        length is known and the position is not shown.
    :param show_pos: show ``position/length``.
    :param item_show_func: returns text describing the current item.
    :param fill_char: character for the finished part.
    :param empty_char: character for the remaining part.
    :param bar_template: format string with ``label``, ``bar`` and
        ``info`` keys.
    :param info_sep: separator between the info fields.
    :param width: bar width in characters; ``0`` uses the terminal width.
    :param file: where to write, stdout by default.
    :param color: keep styling in the output.
    :param update_min_steps: only redraw after this many steps.
    """
    from ._termui_impl import ProgressBar

    color = resolve_color_default(color)
    return ProgressBar(
        iterable=iterable,
        length=length,
        hidden=hidden,
        show_eta=show_eta,
        show_percent=show_percent,
        show_pos=show_pos,
        item_show_func=item_show_func,
        fill_char=fill_char,
        empty_char=empty_char,
        bar_template=bar_template,
        info_sep=info_sep,
        file=file,
        label=label,
        width=width,
        color=color,
        update_min_steps=update_min_steps,
    )


def clear() -> None:
    """Clear the terminal screen, if stdout is a terminal."""
    if not isatty(sys.stdout):
        return
    echo("\033[2J\033[1;1H", nl=False)


def _interpret_color(color: int | tuple[int, int, int] | str, offset: int = 0) -> str:
    if isinstance(color, int):
        return f"{38 + offset};5;{color:d}"
    if isinstance(color, (tuple, list)):
        r, g, b = color
        return f"{38 + offset};2;{r:d};{g:d};{b:d}"
    return str(_ansi_colors[color] + offset)


_STYLE_CODES = {
    "bold": (1, 22),
    "dim": (2, 22),
    "underline": (4, 24),
    "overline": (53, 55),
    "italic": (3, 23),
    "blink": (5, 25),
    "reverse": (7, 27),
    "strikethrough": (9, 29),
}


def style(
    text: t.Any,
    fg: int | tuple[int, int, int] | str | None = None,
    bg: int | tuple[int, int, int] | str | None = None,
    bold: bool | None = None,
    dim: bool | None = None,
    underline: bool | None = None,
    overline: bool | None = None,
    italic: bool | None = None,
    blink: bool | None = None,
    reverse: bool | None = None,
    strikethrough: bool | None = None,
    reset: bool = True,
) -> str:
    """Wrap ``text`` in ANSI codes for colors and text attributes.

    Colors are names such as ``"red"`` or ``"bright_blue"``, an index into
    the 256 color palette, or an ``(r, g, b)`` tuple. Each attribute can be
    switched on (``True``) or explicitly off (``False``).

    :param reset: end with a code that resets all styling.
    """
    if not isinstance(text, str):
        text = str(text)

    codes = []
    for color, offset in ((fg, 0), (bg, 10)):
        if not color:
            continue
        try:
            codes.append(f"\033[{_interpret_color(color, offset)}m")
        except KeyError:
            raise TypeError(f"Unknown color {color!r}") from None

    flags = {
        "bold": bold,
        "dim": dim,
        "underline": underline,
        "overline": overline,
        "italic": italic,
        "blink": blink,
        "reverse": reverse,
        "strikethrough": strikethrough,
    }
    for name, value in flags.items():
        if value is None:
            continue
        on, off = _STYLE_CODES[name]
        codes.append(f"\033[{on if value else off}m")

    codes.append(text)
    if reset:
        codes.append(_ansi_reset_all)
    return "".join(codes)


def unstyle(text: str) -> str:
    """Remove ANSI styling from ``text``."""
    return strip_ansi(text)


def secho(
    message: t.Any | None = None,
    file: t.IO[t.AnyStr] | None = None,
    nl: bool = True,
    err: bool = False,
    color: bool | None = None,
    **styles: t.Any,
) -> None:
    """:func:`style` the message and :func:`echo` it. Bytes are written
    unstyled.
    """
    if message is not None and not isinstance(message, (bytes, bytearray)):
        message = style(message, **styles)
    return echo(message, file=file, nl=nl, err=err, color=color)


@t.overload
def edit(
    text: bytes | bytearray,
    editor: str | None = None,
    env: cabc.Mapping[str, str] | None = None,
    require_save: bool = False,
    extension: str = ".txt",
) -> bytes | None: ...


@t.overload
def edit(
    text: str,
    editor: str | None = None,
    env: cabc.Mapping[str, str] | None = None,
    require_save: bool = True,
    extension: str = ".txt",
) -> str | None: ...


@t.overload
def edit(
    text: None = None,
    editor: str | None = None,
    env: cabc.Mapping[str, str] | None = None,
    require_save: bool = True,
    extension: str = ".txt",
    filename: str | cabc.Iterable[str] | None = None,
) -> None: ...


def edit(
    text: str | bytes | bytearray | None = None,
    editor: str | None = None,
    env: cabc.Mapping[str, str] | None = None,
    require_save: bool = True,
    extension: str = ".txt",
    filename: str | cabc.Iterable[str] | None = None,
) -> str | bytes | bytearray | None:
    """Open ``text`` in the user's editor and return the edited text, or
    ``None`` if the file was closed without saving and ``require_save``
    is set.

    :param editor: command to run; defaults to ``$VISUAL``, ``$EDITOR``
        or a platform default.
    :param env: extra environment variables for the editor.
    :param extension: file extension of the temporary file, which helps
        editors pick syntax highlighting.
    :param filename: edit these existing files instead of text.
    """
    from ._termui_impl import Editor

    ed = Editor(editor=editor, env=env, require_save=require_save, extension=extension)
    if filename is None:
        return ed.edit(text)
    if isinstance(filename, str):
        filename = (filename,)
    ed.edit_files(filename)
    return None


def launch(url: str, wait: bool = False, locate: bool = False) -> int:
    """Open a URL or file with the default application.

    :param wait: wait for the application to exit.
    :param locate: show the file in the file manager instead of opening.
    :return: the exit code of the launcher.
    """
    from ._termui_impl import open_url

    return open_url(url, wait=wait, locate=locate)


# Replaced by the test runner.
_getchar: t.Callable[[bool], str] | None = None


def getchar(echo: bool = False) -> str:
    """Read a single key press from the terminal, without waiting for
    enter. Special keys may produce several characters.

    :param echo: show the character.
    """
    global _getchar
    if _getchar is None:
        from ._termui_impl import getchar as impl

        _getchar = impl
    return _getchar(echo)


def raw_terminal() -> AbstractContextManager[int]:
    from ._termui_impl import raw_terminal as impl

    return impl()


def pause(info: str | None = None, err: bool = False) -> None:
    """Wait for any key press, showing ``info`` meanwhile. Does nothing
    when not running in a terminal.
    """
    if not isatty(sys.stdin) or not isatty(sys.stdout):
        return
    if info is None:
        info = _("Press any key to continue...")
    try:
        if info:
            echo(info, nl=False, err=err)
        try:
            getchar()
        except (KeyboardInterrupt, EOFError):
            pass
    finally:
        if info:
            echo(err=err)
