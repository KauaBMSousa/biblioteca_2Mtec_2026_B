"""Group test failures by apparent root cause (ENHANCEME §12).

27 failing tests are rarely 27 problems. Each pytest failure carries a
one-line `reason` (`AssertionError: assert 3 == 4`, `ImportError: cannot
import name 'foo'`, ...). Normalizing that line — drop the specific
values, keep the exception type and message shape — collapses the list to
the handful of distinct causes actually worth looking at.
"""

import re
from typing import Any

_NUM = re.compile(r"\b\d+\b")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_QUOTED = re.compile(r"(['\"]).*?\1")
_WS = re.compile(r"\s+")
_EXC = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Warning|Failure))\b")


def _signature(reason: str | None) -> tuple[str, str]:
    """`(exception_type, normalized_message)` for one failure `reason` line."""
    if not reason:
        return ("unknown", "no reason reported")
    first = reason.strip().splitlines()[0]
    exc_match = _EXC.match(first)
    exc = exc_match.group(1) if exc_match else "unknown"
    norm = first
    if exc_match:
        norm = first[exc_match.end():].lstrip(": ").strip() or first
    norm = _HEX.sub("0xADDR", norm)
    norm = _QUOTED.sub("'X'", norm)
    norm = _NUM.sub("N", norm)
    norm = _WS.sub(" ", norm).strip()
    return (exc, norm[:200])


def cluster_failures(failures: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse `[{test, reason}]` into `{clusters: [...], cluster_count, failure_count}`.

    Each cluster: `{exception, signature, count, tests, sample_reason}`,
    ordered by descending `count`.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for fail in failures:
        key = _signature(fail.get("reason"))
        bucket = buckets.setdefault(
            key,
            {"exception": key[0], "signature": key[1], "count": 0, "tests": [],
             "sample_reason": fail.get("reason")},
        )
        bucket["count"] += 1
        if len(bucket["tests"]) < 20:
            bucket["tests"].append(fail.get("test"))

    clusters = sorted(buckets.values(), key=lambda b: (-b["count"], b["exception"]))
    return {
        "clusters": clusters,
        "cluster_count": len(clusters),
        "failure_count": len(failures),
    }


__all__ = ["cluster_failures"]
