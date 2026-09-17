"""Defines :class:`ImportInfo`, a single import/include/require reference."""

from dataclasses import dataclass


@dataclass(slots=True)
class ImportInfo:
    """One dependency reference (``import``, ``#include``, ``require``, ...).

    Attributes:
        module: The imported module/header/namespace path as written in
            source.
        line: 1-based source line of the import statement.
    """

    module: str
    line: int
