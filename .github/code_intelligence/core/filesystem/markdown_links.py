"""Markdown link-graph checks: orphaned pages and broken relative links.

Walks every `.md` file under a directory (typically a project's wiki),
builds the graph of relative markdown links between them, and reports two
things a text scan can't answer reliably by eye on anything past a
handful of pages: pages nothing links to (orphans) and links whose target
doesn't resolve to a real file on disk (broken links). Generalizes the ad
hoc `python3 -c "..."` walk this workspace's own wiki-maintenance workflow
has repeated by hand — same regex, same orphan/broken-link definitions,
now a single query instead of a copy-pasted script.

Deliberately does not depend on `core.workspace` (that package's
`__init__` eagerly imports `Workspace`, which itself imports this module
to expose it as a method -- a direct import here would be circular).
Path containment (`resolve_within_workspace`) is the caller's job, same
as `Workspace.read_range` resolving `file` before handing a plain `Path`
down; `base` here is assumed already resolved and contained.
"""

import re
from pathlib import Path
from typing import Any

#: A markdown inline link `[text](target)` whose target ends in `.md`,
#: optionally followed by a `#fragment`. Reference-style links
#: (`[text][ref]`) are intentionally not matched -- this wiki convention
#: uses inline links exclusively, and reference-style would need a second
#: pass over `[ref]: target` definitions to resolve.
_LINK_PATTERN = re.compile(r"\[.*?\]\(([^)]+\.md[^)]*)\)")


def check_markdown_links(
    base: Path,
    index_file: str | None = "Home.md",
) -> dict[str, Any]:
    """Scan every `.md` file under `base` for orphans and broken relative links.

    Args:
        base: Directory to scan, already resolved and containment-checked
            by the caller (see module docstring). Typically a wiki
            directory, e.g. `<workspace_root>/software/nn/.wiki`.
        index_file: Filename (relative to `base`) exempted from the orphan
            check -- a wiki's table-of-contents page is expected to have no
            inbound links. Pass `None` to require every page to have a
            backlink.

    Returns:
        `{"pages_scanned": int, "orphans": [path, ...], "broken_links":
        [{"source", "target", "resolved"}, ...]}`. All paths are POSIX,
        relative to `base`.
    """
    base = Path(base)
    pages = sorted(p.relative_to(base).as_posix() for p in base.rglob("*.md") if p.is_file())
    page_set = set(pages)

    backlinks: dict[str, set[str]] = {p: set() for p in pages}
    broken: list[dict[str, str]] = []

    for src in pages:
        text = (base / src).read_text(encoding="utf-8", errors="replace")
        for match in _LINK_PATTERN.finditer(text):
            href = match.group(1).split("#", 1)[0].strip()
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            target = _normalize(src, href)
            if target in page_set:
                backlinks[target].add(src)
            else:
                broken.append({"source": src, "target": href, "resolved": target})

    orphans = [p for p in pages if not backlinks[p] and p != index_file]

    return {
        "pages_scanned": len(pages),
        "orphans": orphans,
        "broken_links": broken,
    }


def _normalize(source: str, href: str) -> str:
    """Resolve a markdown-relative `href` against `source`'s directory, POSIX-style.

    A leading `/` is treated as root-of-scan-relative (matches how a wiki
    viewer usually interprets it), not as an OS-absolute path -- joining
    `Path` with an absolute operand would otherwise silently discard
    `source`'s directory in a way that's easy to misread as intentional.
    """
    src_dir = Path(source).parent
    href = href.lstrip("/")
    combined = (src_dir / href) if href else Path(source)
    parts: list[str] = []
    for part in combined.as_posix().split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


__all__ = ["check_markdown_links"]
