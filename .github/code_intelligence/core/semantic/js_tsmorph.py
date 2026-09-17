"""JavaScript/TypeScript semantic backend: the TypeScript compiler API.

TypeScript's checker resolves JS as well as TS -- it is the engine behind
every editor's go-to-definition for both -- so one backend serves the
`javascript` language of the index.

The work happens in a small Node script (`_tsc_extract.js`) rather than
from Python, because the compiler API only exists in JavaScript. Python
drives it and reads back JSON: the same shape every other backend returns.
"""

import json
import shutil
import subprocess
from pathlib import Path

from code_intelligence.core.semantic.protocol import BaseBackend, ExtractionResult, SemanticEdge

#: The driver script lives next to this module; it is plain Node with no
#: dependency beyond `typescript` itself.
_SCRIPT = Path(__file__).with_name("_tsc_extract.js")


class JavaScriptTypeScriptBackend(BaseBackend):
    """Exact JS/TS references via the TypeScript compiler API."""

    language = "javascript"
    tool = "TypeScript compiler API (node)"

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        missing: list[str] = []
        if shutil.which("node") is None:
            missing.append("executable 'node' not on PATH")
        if not _SCRIPT.is_file():
            missing.append(f"driver script missing: {_SCRIPT.name}")
        if shutil.which("node") is not None and not self._typescript_available():
            missing.append(
                "the 'typescript' compiler API is not usable from node "
                "(need the 5.x series: npm i typescript@^5 -- version 7 is the "
                "native rewrite and no longer exposes createProgram)"
            )
        return (not missing), missing

    @staticmethod
    def _typescript_available() -> bool:
        """Resolve `typescript` the way the driver script will.

        From the SCRIPT's directory, not the current one: node walks up
        from the requiring file, so a copy installed next to this package
        works for any workspace, and a check run from elsewhere would
        report it missing.
        """
        # TypeScript 7 is the native rewrite: its npm package exports only
        # `version`, not the compiler API this driver uses. Resolving the
        # package is therefore not enough -- the API has to be there, or the
        # backend would crash mid-extraction instead of reporting itself
        # unavailable.
        completed = subprocess.run(
            [
                "node",
                "-e",
                "const ts=require('typescript');"
                "process.exit(typeof ts.createProgram === 'function' ? 0 : 3);",
            ],
            cwd=_SCRIPT.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    def extract(self, root: Path, files: list[str], timeout: int = 600) -> ExtractionResult:
        available, missing = self.availability(root)
        if not available:
            # Refuse rather than return nothing: "no backend" and "no
            # references" are different facts.
            raise RuntimeError("; ".join(missing))

        completed = subprocess.run(
            ["node", str(_SCRIPT), str(root), *files],
            cwd=_SCRIPT.parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"tsc extraction failed: {completed.stderr.strip()[:400]}")

        payload = json.loads(completed.stdout or "{}")
        result = ExtractionResult()
        for edge in payload.get("edges", []):
            result.edges.append(
                SemanticEdge(
                    from_file=edge["from_file"],
                    from_line=edge["from_line"],
                    from_column=edge["from_column"],
                    to_name=edge["to_name"],
                    to_file=edge.get("to_file"),
                    to_line=edge.get("to_line"),
                )
            )
        result.dependencies = payload.get("dependencies", {})
        result.failures = payload.get("failures", {})
        return result


def build() -> JavaScriptTypeScriptBackend:
    return JavaScriptTypeScriptBackend()
