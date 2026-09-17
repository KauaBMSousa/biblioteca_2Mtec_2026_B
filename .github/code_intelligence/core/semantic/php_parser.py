"""PHP semantic backend: nikic/php-parser, the engine Rector is built on.

Rector itself is a refactoring runner; what the index needs is the layer
underneath it -- a real PHP parser with name resolution, which resolves a
call against `use` statements and namespaces instead of guessing from the
bare name.

The work happens in a small PHP script (`_php_extract.php`) driven from
Python, because the parser is a PHP library. Python reads back the same
JSON shape every other backend returns.
"""

import json
import shutil
import subprocess
from pathlib import Path

from code_intelligence.core.semantic.protocol import BaseBackend, ExtractionResult, SemanticEdge

_SCRIPT = Path(__file__).with_name("_php_extract.php")


class PhpParserBackend(BaseBackend):
    """Namespace-resolved PHP references via nikic/php-parser."""

    language = "php"
    tool = "nikic/php-parser (the engine under Rector)"

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        missing: list[str] = []
        if shutil.which("php") is None:
            missing.append("executable 'php' not on PATH")
        if not _SCRIPT.is_file():
            missing.append(f"driver script missing: {_SCRIPT.name}")
        if shutil.which("php") is not None and not self._parser_available(root):
            missing.append(
                "nikic/php-parser is not installed "
                "(composer require nikic/php-parser, in the workspace or globally)"
            )
        return (not missing), missing

    @staticmethod
    def _vendor_autoload(root: Path) -> Path | None:
        """Where composer put its autoloader: workspace first, then global."""
        candidates = [root / "vendor" / "autoload.php"]
        home = Path.home() / ".config" / "composer" / "vendor" / "autoload.php"
        candidates.append(home)
        return next((path for path in candidates if path.is_file()), None)

    def _parser_available(self, root: Path) -> bool:
        """Is nikic/php-parser actually loadable -- not merely "a vendor dir exists".

        Checking for `vendor/autoload.php` alone reported this backend as
        available on a machine where composer had installed something else
        entirely, which is the precise failure this subsystem exists to
        prevent: a backend that claims to be exact and is not.
        """
        autoload = self._vendor_autoload(root)
        if autoload is None:
            return False
        completed = subprocess.run(
            [
                "php",
                "-r",
                f"require '{autoload}'; exit(class_exists('PhpParser\\ParserFactory') ? 0 : 1);",
            ],
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0

    def extract(self, root: Path, files: list[str], timeout: int = 300) -> ExtractionResult:
        available, missing = self.availability(root)
        if not available:
            raise RuntimeError("; ".join(missing))

        autoload = self._vendor_autoload(root)
        completed = subprocess.run(
            ["php", str(_SCRIPT), str(autoload), str(root), *files],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"php extraction failed: {completed.stderr.strip()[:400]}")

        payload = json.loads(completed.stdout or "{}")
        result = ExtractionResult()
        for edge in payload.get("edges", []):
            result.edges.append(
                SemanticEdge(
                    from_file=edge["from_file"],
                    from_line=edge["from_line"],
                    from_column=edge.get("from_column", 1),
                    to_name=edge["to_name"],
                    to_file=edge.get("to_file"),
                    to_line=edge.get("to_line"),
                )
            )
        result.dependencies = payload.get("dependencies", {})
        result.failures = payload.get("failures", {})
        return result


def build() -> PhpParserBackend:
    return PhpParserBackend()
