from dataclasses import replace
from datetime import date

from linear_release.apply import Context, Outcome, ReleaseResult
from linear_release.git_range import Commit
from linear_release.rules import build_plan
from linear_release.summary import render

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


def _release(audience, name, tickets, created=True, released=False, removed=()):
    return ReleaseResult(audience, name, f"https://linear.app/r/{audience}", tickets, created, released, removed)


def _outcome(plan, releases=None, commented=("ENG-2131",)):
    return Outcome(
        releases=releases if releases is not None else (
            _release("astra", "Astra 2026.11.04.2", ("ENG-2103",)),
            _release("internal", "Internal 2026.11.04", ("ENG-2044",)),
        ),
        moved=tuple((m.ticket.identifier, m.to_state) for m in plan.moves),
        commented=commented,
    )


def _in_order(text, *parts):
    positions = [text.index(part) for part in parts]
    return positions == sorted(positions)


def test_summary_sections_in_order():
    """Given a run that made two releases, moved statuses, flagged and skipped tickets and saw stray commits,
    When the summary is rendered,
    Then every section appears in order."""
    plan = _plan()
    text = render(CTX, plan, _outcome(plan), ["ENG-99999"], [Commit("abcdef1234567", "Bump deps")])

    assert _in_order(
        text,
        "Linear release · Helios v1.18.0",
        "v1.17.0 → v1.18.0",
        "### Releases",
        "[Astra 2026.11.04.2](https://linear.app/r/astra)",
        "### Statuses",
        "### Needs attention",
        "### Left out of the release",
        "### Not found in Linear",
        "### Commits with no ticket",
        "### Next steps",
    )
    assert "(dry run)" not in text


def test_summary_lists_releases_as_in_progress_and_statuses_by_target():
    """Given two new releases and the plan's moves,
    When the summary is rendered,
    Then each release reads as In Progress and moves are grouped by status."""
    plan = _plan()
    text = render(CTX, plan, _outcome(plan), [], [])

    assert "| [Astra 2026.11.04.2](https://linear.app/r/astra) | ENG-2103 | In Progress, created by this run |" in text
    assert "| Deployed | ENG-1023, ENG-2044, ENG-2103, ENG-2131 |" in text
    assert "| Done | ENG-1686 |" in text


def test_summary_tells_owners_what_to_fix_and_who_was_told():
    """Given a flagged bug and informational notes,
    When the summary is rendered,
    Then the flagged ticket is listed with what to do, the commented tickets are named, and the rest are listed apart."""
    plan = _plan()
    text = render(CTX, plan, _outcome(plan), [], [])

    assert "| ENG-2131 | no Release Method label | Add `immediate` or `gated`" in text
    assert "Commented on ENG-2131 in Linear" in text
    assert "| ENG-1686 | sub-issue of ENG-1368 |" in text
    assert "| ENG-1023 | gated |" in text


def test_summary_next_steps_point_at_each_release():
    """Given two releases awaiting review,
    When the summary is rendered,
    Then the next steps send the deployer to release each one in Linear and post its notes."""
    plan = _plan()
    text = render(CTX, plan, _outcome(plan), [], [])

    assert "move it to **Released**" in text
    assert "[Internal 2026.11.04](https://linear.app/r/internal)" in text.split("### Next steps")[1]
    assert "#releases" in text


def test_summary_marks_dry_runs():
    """Given a dry run,
    When the summary is rendered,
    Then the title says so, releases would be created, and comments would be posted."""
    plan = _plan()
    dry = (_release("astra", "Astra 2026.11.04.2", ("ENG-2103",)),)
    text = render(replace(CTX, dry_run=True), plan, _outcome(plan, releases=dry), [], [])

    assert "Linear release · Helios v1.18.0 (dry run)" in text
    assert "would be created" in text
    assert "A real run would comment on ENG-2131" in text


def test_summary_reports_removed_tickets_and_empty_releases():
    """Given a re-run that removed tickets, emptying one release,
    When the summary is rendered,
    Then the removals are listed and the empty release is called out for cancelling."""
    plan = _plan()
    releases = (
        _release("astra", "Astra 2026.11.04.2", ("ENG-2103",), created=False, removed=("ENG-3",)),
        _release("internal", "Internal 2026.11.04", (), created=False, removed=("ENG-4",)),
    )
    text = render(CTX, plan, _outcome(plan, releases=releases), [], [])

    assert "Removed from Astra 2026.11.04.2: ENG-3" in text
    assert "Internal 2026.11.04 has no tickets left. Cancel it in Linear." in text


def test_summary_warns_when_tickets_join_an_already_released_release():
    """Given a person already released this deploy's release before a re-run added tickets,
    When the summary is rendered,
    Then the deployer is told to regenerate its notes."""
    plan = _plan()
    releases = (_release("astra", "Astra 2026.11.04.2", ("ENG-2103",), created=False, released=True),)
    text = render(CTX, plan, _outcome(plan, releases=releases), [], [])

    assert "| Released |" in text
    assert "regenerate its notes" in text


def test_summary_without_releases_says_so():
    """Given a run where no ticket qualified,
    When the summary is rendered,
    Then it says no release was made and has no next steps."""
    plan = build_plan([ticket("ENG-3", "gated", "story")], {}, CFG)
    text = render(CTX, plan, Outcome(releases=(), moved=(("ENG-3", "Deployed"),), commented=()), [], [])

    assert "No release: no ticket qualified." in text
    assert "### Next steps" not in text
