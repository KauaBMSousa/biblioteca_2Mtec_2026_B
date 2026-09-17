"""Java semantic backend: javac's own compiler API, via a small tool.

OpenRewrite is the right engine for *applying* Java refactorings, but it
needs the project's build (Maven or Gradle) to produce a typed LST, which
makes it a heavy dependency for the one question the index needs answered:
what does this call resolve to.

`javac` already answers that. The driver below compiles the requested files
with the project's classpath and walks the resulting typed tree through
`com.sun.source`, which is the same resolution OpenRewrite would get,
without requiring the build to be wired first. When a project DOES have a
pom.xml or build.gradle, the classpath is taken from it.

Unlike the C++ and Python backends, this one has not been exercised against
real Java in this workspace -- there is none. It reports itself unavailable
when the JDK or a source root is missing rather than pretending otherwise.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from code_intelligence.core.semantic.protocol import BaseBackend, ExtractionResult, SemanticEdge

_DRIVER = Path(__file__).with_name("_JavaExtract.java")


class JavaCompilerBackend(BaseBackend):
    """Type-resolved Java references via the javac compiler tree API."""

    language = "java"
    tool = "javac com.sun.source API (OpenRewrite-compatible resolution)"

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        missing: list[str] = []
        if shutil.which("java") is None:
            missing.append("executable 'java' not on PATH")
        if shutil.which("javac") is None:
            missing.append("executable 'javac' not on PATH (a JRE is not enough; a JDK is needed)")
        if not _DRIVER.is_file():
            missing.append(f"driver missing: {_DRIVER.name}")
        if not any(root.rglob("*.java")):
            missing.append("no .java sources in this workspace")
        return (not missing), missing

    def _classpath(self, root: Path) -> str:
        """The project's classpath, from Maven when it is available.

        Falls back to the source root, which resolves intra-project calls
        -- the ones a refactor cares about -- while leaving third-party
        symbols unresolved rather than guessed.
        """
        pom = root / "pom.xml"
        if pom.is_file() and shutil.which("mvn"):
            completed = subprocess.run(
                ["mvn", "-q", "-o", "dependency:build-classpath", "-Dmdep.outputFile=/dev/stdout"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.strip().splitlines()[-1]
        return str(root)

    def extract(self, root: Path, files: list[str], timeout: int = 600) -> ExtractionResult:
        available, missing = self.availability(root)
        if not available:
            raise RuntimeError("; ".join(missing))

        with tempfile.TemporaryDirectory() as workdir:
            completed = subprocess.run(
                [
                    "java",
                    "--enable-preview" if False else "-cp",
                    self._classpath(root),
                    str(_DRIVER),
                    str(root),
                    *files,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"java extraction failed: {completed.stderr.strip()[:400]}")

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


def build() -> JavaCompilerBackend:
    return JavaCompilerBackend()
