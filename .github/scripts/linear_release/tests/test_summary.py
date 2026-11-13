from dataclasses import replace
from datetime import date

from linear_release.apply import Context, Outcome, ReleaseResult
from linear_release.git_range import Commit
from linear_release.rules import build_plan
from linear_release.summary import PENDING_NOTES, render

from .factories import CFG, child, ticket

CTX = Context(
    service="Helios",
    tag="v1.18.0",
    previous_tag="v1.17.0",
    commit_sha="e13398f6",
    compare_url="https://github.com/AstraFinance/astra-helios/compare/v1.17.0...v1.18.0",
    today=date(2026, 11, 4),
    dry_run=False,
)


def _plan():
    parent = ticket("ENG-1368", "dual-sponsor", children=(child("ENG-2264", "Todo", "unstarted"),))
    return build_plan(
        [
            ticket("ENG-2103", "bug", "immediate"),
            ticket("ENG-2044", "internal", "immediate"),
            ticket("ENG-1023", "gated", "story"),
            ticket("ENG-1686", "dual-sponsor", parent="ENG-1368"),
            ticket("ENG-2131", "bug"),
        ],
        {"ENG-1368": parent},
        CFG,
    )


def _outcome(plan, internal_status="completed"):
    return Outcome(
        releases=(
            ReleaseResult("astra", "Astra 2026.11.04.2", "https://linear.app/r/astra", ("ENG-2103",), True,
                          "What changed\n* ENG-2103: Percentiles are accurate.", "completed"),
            ReleaseResult("internal", "Internal 2026.11.04", "https://linear.app/r/internal", ("ENG-2044",), True,
                          None, internal_status),
        ),
        moved=tuple((m.ticket.identifier, m.to_state) for m in plan.moves),
    )


def _in_order(text, *parts):
    positions = [text.index(part) for part in parts]
    return positions == sorted(positions)


def test_summary_sections():
    """Given a run that made two releases, moved statuses, skipped tickets and saw stray commits,
    When the summary is rendered,
    Then every section appears in order with the details a deployer needs."""
    plan = _plan()
    text = render(CTX, plan, _outcome(plan, internal_status="pending"), ["ENG-99999"],
                  [Commit("abcdef1234567", "Bump deps")])

    assert _in_order(
        text,
        "Linear release · Helios v1.18.0",
        "v1.17.0 → v1.18.0",
        "### Releases",
        "[Astra 2026.11.04.2](https://linear.app/r/astra)",
        "### Statuses",
        "### Not in a release",
        "### Not found in Linear",
        "### Commits with no ticket",
        "### Notes for #releases",
        "Astra 2026.11.04.2 · https://linear.app/r/astra",
        "ENG-2103: Percentiles are accurate.",
        "Internal 2026.11.04 · https://linear.app/r/internal",
        PENDING_NOTES,
    )
    assert "(dry run)" not in text
    assert "| Done | ENG-1686, ENG-2044, ENG-2103 |" in text
    assert "| Deployed | ENG-1023, ENG-2131 |" in text
    assert "| ⚠️ ENG-2131 | no Release Method label |" in text
    assert "| ENG-1686 | sub-issue of ENG-1368 |" in text
    assert "| ENG-1368 | still has open sub-issues: ENG-2264 |" in text
    assert "ENG-99999" in text
    assert "`abcdef1` Bump deps" in text


def test_summary_marks_dry_runs_and_omits_empty_sections():
    """Given a dry run with nothing unfound and no stray commits,
    When the summary is rendered,
    Then the title says dry run and the empty sections are left out."""
    plan = _plan()
    text = render(replace(CTX, dry_run=True), plan, _outcome(plan), [], [])

    assert "Linear release · Helios v1.18.0 (dry run)" in text
    assert "### Not found in Linear" not in text
    assert "### Commits with no ticket" not in text


def test_summary_without_releases_says_so():
    """Given a run where no ticket qualified,
    When the summary is rendered,
    Then it says no release was made and has no notes section."""
    plan = build_plan([ticket("ENG-3", "gated", "story")], {}, CFG)
    text = render(CTX, plan, Outcome(releases=(), moved=(("ENG-3", "Deployed"),)), [], [])

    assert "No release: no ticket qualified." in text
    assert "### Notes for #releases" not in text
