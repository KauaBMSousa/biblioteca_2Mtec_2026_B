"""Defines :class:`IdentifierRef`, a reference to a named identifier."""

from dataclasses import dataclass


@dataclass(slots=True)
class IdentifierRef:
    """A single identifier occurrence discovered during static analysis.

    Attributes:
        name: The identifier's literal text (e.g. a variable, parameter or
            function name).
        kind: What kind of identifier this is (e.g. ``"variable"``,
            ``"parameter"``, ``"function"``, ``"class"``, ``"loop_index"``).
        line: 1-based source line on which the identifier is declared.
        is_exception: True when this identifier is exempt from the minimum
            naming-length rule (e.g. a configured loop-index name like
            ``i``, ``j``, ``k``).
        column: 1-based source column on which the identifier starts.
            Defaults to 1 for call sites that predate column tracking.
    """

    name: str
    kind: str
    line: int
    is_exception: bool = False
    column: int = 1
