"""Text wrapping that measures visible width, so ANSI styling does not
count against the line length and escape sequences are never split.

The standard library's :mod:`textwrap` is only imported on first use, to
keep ``import click`` light.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t
from contextlib import contextmanager

from ._compat import _ansi_re
from ._compat import term_len

def _truncate_visible(text: str, n: int) -> str:
    """Return the start of ``text`` containing ``n`` visible characters.
    Escape sequences before the cut are kept whole; the ones after it are
    dropped.
    """
    if n <= 0:
        return ""
    out = []
    seen = 0
    pos = 0
    while pos < len(text) and seen < n:
        match = _ansi_re.match(text, pos)
        if match is not None:
            out.append(match.group())
            pos = match.end()
            continue
        out.append(text[pos])
        seen += 1
        pos += 1
    return "".join(out)


_wrapper_class: type[t.Any] | None = None


def new_wrapper(**kwargs: t.Any) -> t.Any:
    """Create a wrapper, a :class:`textwrap.TextWrapper` that measures
    visible width. It also supports temporary extra indentation
    (``extra_indent``) and indenting lines without re-wrapping them
    (``indent_only``).
    """
    return _get_wrapper_class()(**kwargs)


def _get_wrapper_class() -> type[t.Any]:
    global _wrapper_class
    if _wrapper_class is not None:
        return _wrapper_class

    import textwrap

    class _VisibleWidthWrapper(textwrap.TextWrapper):
        def _handle_long_word(
            self,
            reversed_chunks: list[str],
            cur_line: list[str],
            cur_len: int,
            width: int,
        ) -> None:
            space_left = max(width - cur_len, 1)
            if self.break_long_words:
                chunk = reversed_chunks[-1]
                head = _truncate_visible(chunk, space_left)
                cur_line.append(head)
                reversed_chunks[-1] = chunk[len(head) :]
            elif not cur_line:
                cur_line.append(reversed_chunks.pop())

        def _wrap_chunks(self, chunks: list[str]) -> list[str]:
            if self.width <= 0:
                raise ValueError(f"invalid width {self.width!r} (must be > 0)")

            lines: list[str] = []
            chunks = list(reversed(chunks))

            while chunks:
                line: list[str] = []
                used = 0
                indent = self.subsequent_indent if lines else self.initial_indent
                width = self.width - term_len(indent)

                # A line never starts with whitespace, except the first.
                if self.drop_whitespace and lines and not chunks[-1].strip():
                    del chunks[-1]

                while chunks:
                    size = term_len(chunks[-1])
                    if used + size > width:
                        break
                    line.append(chunks.pop())
                    used += size

                if chunks and term_len(chunks[-1]) > width:
                    self._handle_long_word(chunks, line, used, width)
                    used = sum(term_len(c) for c in line)

                if self.drop_whitespace and line and not line[-1].strip():
                    used -= term_len(line[-1])
                    del line[-1]

                if line:
                    lines.append(indent + "".join(line))

            return lines

        @contextmanager
        def extra_indent(self, indent: str) -> cabc.Iterator[None]:
            saved = (self.initial_indent, self.subsequent_indent)
            self.initial_indent += indent
            self.subsequent_indent += indent
            try:
                yield
            finally:
                self.initial_indent, self.subsequent_indent = saved

        def indent_only(self, text: str) -> str:
            out = []
            for idx, line in enumerate(text.splitlines()):
                prefix = self.initial_indent if idx == 0 else self.subsequent_indent
                out.append(f"{prefix}{line}")
            return "\n".join(out)

    _wrapper_class = _VisibleWidthWrapper
    return _wrapper_class
