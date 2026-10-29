"""Internal sentinel values.

``UNSET`` marks "no value was provided", which must stay distinguishable
from an explicit ``None``. ``FLAG_NEEDS_VALUE`` marks an option with an
optional value that was given without one.
"""

from __future__ import annotations

import enum
import typing as t


class Sentinel(enum.Enum):
    """Singleton markers. Each member is unique and truthy, and survives
    copying and pickling as the same object.
    """

    UNSET = object()
    FLAG_NEEDS_VALUE = object()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}.{self.name}"

    def __str__(self) -> str:
        return repr(self)

    def __reduce_ex__(self, protocol: t.SupportsIndex) -> t.Any:
        # The default enum reduction re-creates members from their value,
        # which fails for a bare ``object()``. Look them up by name instead.
        return (getattr, (Sentinel, self.name))

    def __copy__(self) -> Sentinel:
        return self

    def __deepcopy__(self, memo: dict[int, t.Any]) -> Sentinel:
        return self


UNSET = Sentinel.UNSET
"""No value was given."""

FLAG_NEEDS_VALUE = Sentinel.FLAG_NEEDS_VALUE
"""An option that takes an optional value was given without one."""

T_UNSET = t.Literal[UNSET]  # type: ignore[valid-type]
T_FLAG_NEEDS_VALUE = t.Literal[FLAG_NEEDS_VALUE]  # type: ignore[valid-type]
