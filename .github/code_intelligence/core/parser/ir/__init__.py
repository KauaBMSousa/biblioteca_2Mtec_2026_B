"""Language-agnostic intermediate representation (IR) produced by adapters.

Ported from `tools/code_quality/code_quality/ir/` (minus `SymbolRecord` and
`Violation`, which now live in `core/symbols/records.py` and
`core/diagnostics/violation.py` respectively, per the plan's module split).
"""

from code_intelligence.core.parser.ir.class_info import ClassInfo
from code_intelligence.core.parser.ir.file_analysis import FileAnalysis
from code_intelligence.core.parser.ir.function_info import FunctionInfo, Token
from code_intelligence.core.parser.ir.identifier_ref import IdentifierRef
from code_intelligence.core.parser.ir.import_info import ImportInfo

__all__ = [
    "ClassInfo",
    "FileAnalysis",
    "FunctionInfo",
    "IdentifierRef",
    "ImportInfo",
    "Token",
]
