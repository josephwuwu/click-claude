"""Help page rendering: text wrapping, indentation and definition lists."""

from __future__ import annotations

import collections.abc as cabc
from contextlib import contextmanager
from gettext import gettext as _

from ._compat import term_len
from ._textwrap import new_wrapper
from .parser import _split_opt

#: When set, every :class:`HelpFormatter` uses this width instead of looking
#: at the terminal. Mostly useful for tests.
FORCED_WIDTH: int | None = None


def measure_table(rows: cabc.Iterable[tuple[str, str]]) -> tuple[int, ...]:
    """Return the display width of each column of ``rows``."""
    widths: list[int] = []
    for row in rows:
        for idx, cell in enumerate(row):
            size = term_len(cell)
            if idx < len(widths):
                widths[idx] = max(widths[idx], size)
            else:
                widths.append(size)
    return tuple(widths)


def iter_rows(
    rows: cabc.Iterable[tuple[str, str]], col_count: int
) -> cabc.Iterator[tuple[str, ...]]:
    """Yield each row padded with empty cells to ``col_count`` columns."""
    for row in rows:
        yield tuple(row) + ("",) * (col_count - len(row))


def _paragraphs(text: str) -> list[tuple[int, bool, str]]:
    """Split ``text`` on blank lines into ``(indent, raw, text)`` triples.
    A paragraph that starts with a ``\\b`` line is raw and must not be
    re-wrapped.
    """
    result: list[tuple[int, bool, str]] = []
    lines: list[str] = []
    indent: int | None = None

    def flush() -> None:
        if not lines:
            return
        if lines[0].strip() == "\b":
            result.append((indent or 0, True, "\n".join(lines[1:])))
        else:
            result.append((indent or 0, False, " ".join(lines)))
        lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            indent = None
            continue
        if indent is None:
            stripped = line.lstrip()
            indent = term_len(line) - term_len(stripped)
            line = stripped
        lines.append(line)
    flush()
    return result


def wrap_text(
    text: str,
    width: int = 78,
    initial_indent: str = "",
    subsequent_indent: str = "",
    preserve_paragraphs: bool = False,
) -> str:
    """Wrap ``text`` to ``width`` columns.

    With ``preserve_paragraphs`` each blank-line separated paragraph is
    wrapped on its own and keeps its indentation, and paragraphs that start
    with a line containing only ``\\b`` are left exactly as written.
    """
    text = text.expandtabs()
    wrapper = new_wrapper(
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        replace_whitespace=False,
    )
    if not preserve_paragraphs:
        return wrapper.fill(text)

    out = []
    for indent, raw, body in _paragraphs(text):
        with wrapper.extra_indent(" " * indent):
            out.append(wrapper.indent_only(body) if raw else wrapper.fill(body))
    return "\n\n".join(out)


class HelpFormatter:
    """Builds help output in a buffer while tracking the current indent.

    :param indent_increment: columns added by each :meth:`indent`.
    :param width: total width; defaults to the terminal width, capped at
        ``max_width`` and at least 50.
    :param max_width: cap for the automatic width, 80 by default.
    """

    def __init__(
        self,
        indent_increment: int = 2,
        width: int | None = None,
        max_width: int | None = None,
    ) -> None:
        self.indent_increment = indent_increment
        if max_width is None:
            max_width = 80
        if width is None:
            width = FORCED_WIDTH
            if width is None:
                import shutil

                columns = shutil.get_terminal_size().columns
                width = max(min(columns, max_width) - 2, 50)
        self.width = width
        self.current_indent = 0
        self.buffer: list[str] = []

    def write(self, string: str) -> None:
        self.buffer.append(string)

    def indent(self) -> None:
        self.current_indent += self.indent_increment

    def dedent(self) -> None:
        self.current_indent -= self.indent_increment

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        """Write a usage line like ``Usage: prog [OPTIONS]``. Long argument
        lists are wrapped and aligned after the program name.
        """
        if prefix is None:
            prefix = f"{_('Usage:')} "

        if not args:
            self.write(f"{prefix:>{self.current_indent}}{prog}\n")
            return

        lead = f"{prefix:>{self.current_indent}}{prog} "
        avail = self.width - self.current_indent

        if avail >= term_len(lead) + 20:
            pad = " " * term_len(lead)
            self.write(wrap_text(args, avail, initial_indent=lead, subsequent_indent=pad))
        else:
            # The program name eats most of the line; put the arguments on
            # their own lines below it.
            self.write(lead)
            self.write("\n")
            pad = " " * (max(self.current_indent, term_len(prefix)) + 4)
            self.write(wrap_text(args, avail, initial_indent=pad, subsequent_indent=pad))
        self.write("\n")

    def write_heading(self, heading: str) -> None:
        self.write(f"{'':>{self.current_indent}}{heading}:\n")

    def write_paragraph(self) -> None:
        """Start a new paragraph by writing a blank line, unless nothing has
        been written yet.
        """
        if self.buffer:
            self.write("\n")

    def write_text(self, text: str) -> None:
        pad = " " * self.current_indent
        self.write(
            wrap_text(
                text,
                self.width,
                initial_indent=pad,
                subsequent_indent=pad,
                preserve_paragraphs=True,
            )
        )
        self.write("\n")

    def write_dl(
        self,
        rows: cabc.Sequence[tuple[str, str]],
        col_max: int = 30,
        col_spacing: int = 2,
    ) -> None:
        """Write a two column definition list, such as the options of a
        command and their help texts.

        :param col_max: widest the first column may be; longer terms push
            their definition to the next line.
        :param col_spacing: gap between the columns.
        """
        rows = list(rows)
        widths = measure_table(rows)
        if len(widths) != 2:
            raise TypeError("Expected two columns for definition list")

        term_col = min(widths[0], col_max) + col_spacing
        definition_indent = " " * (term_col + self.current_indent)

        for term, definition in iter_rows(rows, len(widths)):
            self.write(f"{'':>{self.current_indent}}{term}")
            if not definition:
                self.write("\n")
                continue

            used = term_len(term)
            if used <= term_col - col_spacing:
                self.write(" " * (term_col - used))
            else:
                self.write("\n")
                self.write(definition_indent)

            text_width = max(self.width - term_col - 2, 10)
            lines = wrap_text(definition, text_width, preserve_paragraphs=True).splitlines()
            if not lines:
                self.write("\n")
                continue
            self.write(f"{lines[0]}\n")
            for line in lines[1:]:
                self.write(f"{definition_indent}{line}\n")

    @contextmanager
    def section(self, name: str) -> cabc.Iterator[None]:
        """Write a heading and indent everything written inside the block."""
        self.write_paragraph()
        self.write_heading(name)
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    @contextmanager
    def indentation(self) -> cabc.Iterator[None]:
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    def getvalue(self) -> str:
        return "".join(self.buffer)


def join_options(options: cabc.Sequence[str]) -> tuple[str, bool]:
    """Join option names for display, shortest prefix first. Also reports
    whether any of them uses ``/`` as prefix, which affects how the
    secondary names of a flag are shown.
    """
    keyed = []
    slash = False
    for opt in options:
        prefix = _split_opt(opt)[0]
        if prefix == "/":
            slash = True
        keyed.append((len(prefix), opt))
    keyed.sort(key=lambda item: item[0])
    return ", ".join(opt for _, opt in keyed), slash
