"""Every function and method should have at least one test that reaches it.

This rule cannot live with the others in `run_file_rules`. Those look at one
`FileAnalysis` at a time, and "is anything testing this?" is a question about
a DIFFERENT file -- the test. So it runs as a project-wide pass, next to
duplication detection, and reads the semantic index rather than the parse
tree.

That dependency is what makes the rule honest, and also what makes it
refusable. Three states, and only two of them are findings:

    exact index complete, symbol not reached by any test  -> UNTESTED_SYMBOL
    exact index complete, symbol reached                  -> nothing
    exact index incomplete or absent                      -> TEST_COVERAGE_UNKNOWN

The third row is the one worth arguing about. The tempting alternative is to
emit nothing when the index is incomplete, which is wrong in the direction
that hurts: "no findings" reads as "everything is tested". An incomplete
index cannot distinguish a function whose test was never extracted from a
function with no test, so it says so out loud, once, and names the command
that fixes it.
"""

from typing import Any

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.index.store import IndexStore

#: Emitted once per incomplete language, not once per symbol: the reader
#: needs to run one command, not read the same sentence 4000 times.
UNKNOWN_CODE = "TEST_COVERAGE_UNKNOWN"
UNTESTED_CODE = "UNTESTED_SYMBOL"


def _severity_from_config(name: str) -> Severity:
    """Resolve a configured severity name, refusing an unknown one.

    No fallback to a default: a typo in `"severity": "HGIH"` would otherwise
    silently downgrade every finding of this rule to whatever the default is,
    and the config would keep claiming otherwise.
    """
    try:
        return Severity[name.upper()]
    except KeyError as exc:
        valid = ", ".join(level.name for level in Severity)
        raise ValueError(
            f"untested_symbols.severity: unknown severity {name!r}; expected one of {valid}"
        ) from exc


def check(store: IndexStore, pending: dict[str, Any], config: dict) -> list[Violation]:
    """Findings for functions no test reaches, or one refusal per language.

    `pending` is `Workspace._pending_units()`. `config` is the workspace
    config; the `untested_symbols` block controls enablement, severity, and
    the two noise filters (`min_loc`, `include_private`).
    """
    # Imported here, not at module scope: `core.context.__init__` reaches
    # `core.workspace`, which imports `Indexer`, which imports this module.
    # At module scope that cycle is only survivable when something else
    # happens to import Workspace first -- so `import Indexer` alone would
    # raise, and which of the two entry points a caller picked would decide
    # whether the package imports at all.
    from code_intelligence.core.context import testcoverage

    settings = config.get("untested_symbols", {})
    if not settings.get("enabled", True):
        return []

    severity = _severity_from_config(settings.get("severity", "WARNING"))
    min_loc = settings.get("min_loc", 5)
    include_private = settings.get("include_private", False)

    languages_in_scope = sorted(
        {row["language"] for row in store.list_files() if row["language"] in pending}
    )
    incomplete = testcoverage.incomplete_languages(pending, languages_in_scope)
    # Three states, not two, and only the middle one is a finding:
    #
    #   nothing extracted yet   the semantic pass is a separate, expensive
    #                           command (~65 min for this project's C++), and
    #                           not having run it is a normal state, not a
    #                           defect. Warning here would make every fresh
    #                           index non-OK and flip CI exit codes on repos
    #                           that never opted into semantic indexing.
    #   partly extracted        DANGEROUS: an answer computed from half an
    #                           index looks exactly like an answer computed
    #                           from a whole one. Warn.
    #   fully extracted         run the real check.
    #
    # Asking `untested_symbols` directly still refuses loudly in the first
    # state -- silence here is about not manufacturing a violation, not about
    # pretending the check ran.
    half_built = {
        name: info
        for name, info in incomplete.items()
        if info.get("available")
        and (
            info.get("units_failed", 0) > 0
            or 0 < info.get("units_pending", 0) < info.get("units_claimed", 0)
        )
    }
    if half_built:
        # The gap is in the index, not in any one file, but a diagnostic row
        # must name a file that exists (`diagnostics.file_path` is a foreign
        # key into `files`, and foreign keys are enforced). So it is anchored
        # on the first file of the affected language: arbitrary as a location,
        # correct as a scope, and the message says the check is workspace-wide.
        first_file_of: dict[str, str] = {}
        for row in store.list_files():
            first_file_of.setdefault(row["language"], row["path"])
        return [
            Violation(
                code=UNKNOWN_CODE,
                severity=Severity.WARNING,
                file=first_file_of[name],
                line=1,
                end_line=1,
                message=(
                    f"test coverage could not be checked for {name}: "
                    + testcoverage.describe_incompleteness({name: info})
                ),
                detail={"language": name, "scope": "workspace"},
                confidence="high",
            )
            for name, info in sorted(half_built.items())
            if name in first_file_of
        ]

    # Incomplete for any other reason (never started, no backend installed):
    # emit nothing rather than raising. The rule runs on every index() and
    # must not turn a normal, opted-out state into a failed index -- asking
    # `untested_symbols` directly is where the refusal belongs.
    if incomplete:
        return []

    report = testcoverage.untested_symbols(
        store,
        pending,
        min_loc=min_loc,
        include_private=include_private,
        limit=len(store.list_all_symbols()) or 1,
    )

    return [
        Violation(
            code=UNTESTED_CODE,
            severity=severity,
            file=candidate["file"],
            line=candidate["line"],
            end_line=candidate["end_line"],
            message=(
                f"{candidate['qualified_name']} ({candidate['loc']} lines) is not reached "
                "by any test file"
            ),
            detail={
                "function": candidate["qualified_name"],
                "physical_lines": candidate["loc"],
                "cyclomatic_complexity": candidate["complexity"],
            },
            symbol_id=candidate["symbol_id"],
            # "High" is about the EDGE, not about the judgement: the index
            # knows for certain that no test file reaches this symbol. Whether
            # that means it needs a test is the reader's call -- see the
            # caveats on untested_symbols().
            confidence="high",
        )
        for candidate in report["candidates"]
    ]
