#!/usr/bin/env python3
"""Entrypoint shim: `python code_intelligence/code-intelligence.py index PATH`.

Delegates to `code_intelligence.cli.main.main`. Kept intentionally thin —
all real logic lives in the `code_intelligence` package. Mirrors
`tools/code_quality/code_quality.py`'s shim convention: inserts this
package's *parent* directory onto `sys.path` (not this directory itself),
since `code_intelligence` is the importable package name, matching every
internal `from code_intelligence.core...` import.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from code_intelligence.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
