from linear_release.model import Note, ReleaseRef
from linear_release.rules import build_plan

from .factories import CFG, child, ids, moves, note, ticket


def test_immediate_story_goes_to_astra_and_done():
    """Given an immediate top-level story ready for deployment,
    When the plan is built,
    Then it is in the Astra release and moves to Done."""
    plan = build_plan([ticket("ENG-1", "immediate", "story")], {}, CFG)
    assert ids(plan.releases["astra"]) == ["ENG-1"]
    assert moves(plan) == {"ENG-1": "Done"}


def test_internal_ticket_of_any_type_goes_to_internal():
    """Given an immediate internal ticket typed as a task,
    When the plan is built,
    Then it is in the Internal release and no Astra release exists."""
    plan = build_plan([ticket("ENG-2", "immediate", "internal", "task")], {}, CFG)
    assert ids(plan.releases["internal"]) == ["ENG-2"]
    assert "astra" not in plan.releases


def test_both_audiences_from_one_deploy():
    """Given a client-facing bug and an internal ticket in one deploy,
    When the plan is built,
    Then both an Astra and an Internal release are planned."""
    plan = build_plan(
        [ticket("ENG-1", "immediate", "bug"), ticket("ENG-2", "immediate", "internal")], {}, CFG
    )
    assert set(plan.releases) == {"astra", "internal"}


def test_gated_is_deployed_and_not_released():
    """Given a gated story,
    When the plan is built,
    Then it moves to Deployed and waits for its gate outside any release."""
    plan = build_plan([ticket("ENG-3", "gated", "story")], {}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {"ENG-3": "Deployed"}


def test_immediate_task_is_done_but_not_released():
    """Given an immediate task with no story or bug label,
    When the plan is built,
    Then it is finished but left out of the release, with a plain note."""
    plan = build_plan([ticket("ENG-4", "immediate", "task")], {}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {"ENG-4": "Done"}
    assert note(plan, "ENG-4") == Note("ENG-4", "no story or bug label", attention=False)


def test_bug_without_release_method_is_deployed_and_flagged():
    """Given a bug with no Release Method label,
    When the plan is built,
    Then it stops at Deployed and is flagged for attention."""
    plan = build_plan([ticket("ENG-5", "bug")], {}, CFG)
    assert moves(plan) == {"ENG-5": "Deployed"}
    assert note(plan, "ENG-5") == Note("ENG-5", "no Release Method label", attention=True)


def test_untyped_ticket_without_release_method_is_done():
    """Given a ticket with neither a type nor a Release Method label,
    When the plan is built,
    Then it is finished because nothing requires it to be gated."""
    plan = build_plan([ticket("ENG-6", "dual-sponsor")], {}, CFG)
    assert moves(plan) == {"ENG-6": "Done"}
    assert plan.releases == {}


def test_advance_notice_is_deployed_flagged_and_not_released():
    """Given an immediate story that requires advance notice,
    When the plan is built,
    Then it is left to its planned release, at Deployed, and flagged."""
    plan = build_plan(
        [ticket("ENG-7", "immediate", "story", "advance-notice-required")], {}, CFG
    )
    assert plan.releases == {}
    assert moves(plan) == {"ENG-7": "Deployed"}
    assert note(plan, "ENG-7").attention


def test_ticket_not_ready_is_left_alone_and_flagged():
    """Given a shipped ticket that is still In Review,
    When the plan is built,
    Then its status is untouched, it is not released, and it is flagged."""
    plan = build_plan([ticket("ENG-8", "immediate", "story", state="In Review")], {}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {}
    assert note(plan, "ENG-8") == Note("ENG-8", "In Review, not Ready for Deployment", attention=True)


def test_done_ticket_not_yet_released_is_released_without_a_move():
    """Given an immediate story someone already moved to Done,
    When the plan is built,
    Then it is released and its status is left as it is."""
    plan = build_plan(
        [ticket("ENG-9", "immediate", "story", state="Done", state_type="completed")], {}, CFG
    )
    assert ids(plan.releases["astra"]) == ["ENG-9"]
    assert moves(plan) == {}


def test_ticket_in_another_completed_release_is_skipped():
    """Given a ticket already in an earlier completed release, as after a hotfix forward-port,
    When the plan is built,
    Then it is neither released again nor moved."""
    t = ticket(
        "ENG-10", "immediate", "bug", state="Done",
        completed_releases=(ReleaseRef("r1", "Astra 2026.11.12"),),
    )
    plan = build_plan([t], {}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {}
    assert note(plan, "ENG-10").reason == "already in Astra 2026.11.12"


def test_own_release_does_not_count_as_already_released():
    """Given a ticket already in this run's own release from an earlier attempt,
    When the plan is built for a re-run,
    Then the ticket stays in that release."""
    t = ticket(
        "ENG-11", "immediate", "bug", state="Done",
        completed_releases=(ReleaseRef("own", "Astra 2026.11.13"),),
    )
    plan = build_plan([t], {}, CFG, own_release_ids=frozenset({"own"}))
    assert ids(plan.releases["astra"]) == ["ENG-11"]


def test_sub_issue_is_done_not_released_and_parent_with_open_child_stays_out():
    """Given a shipped sub-issue whose parent still has another open sub-issue,
    When the plan is built,
    Then the sub-issue is finished, and neither it nor the parent is released."""
    parent = ticket(
        "ENG-20", "immediate", "story", state="In Progress",
        children=(child("ENG-21", "Ready for Deployment"), child("ENG-22", "Todo", "unstarted")),
    )
    plan = build_plan([ticket("ENG-21", "immediate", parent="ENG-20")], {"ENG-20": parent}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {"ENG-21": "Done"}
    assert note(plan, "ENG-21").reason == "sub-issue of ENG-20"
    assert note(plan, "ENG-20").reason == "still has open sub-issues: ENG-22"


def test_last_sub_issue_releases_its_parent():
    """Given the last open sub-issue of an immediate story ships,
    When the plan is built,
    Then the parent story is released and both move to Done."""
    parent = ticket(
        "ENG-30", "immediate", "story", state="In Progress",
        children=(child("ENG-31", "Ready for Deployment"), child("ENG-32", "Done", "completed")),
    )
    plan = build_plan([ticket("ENG-31", "immediate", parent="ENG-30")], {"ENG-30": parent}, CFG)
    assert ids(plan.releases["astra"]) == ["ENG-30"]
    assert moves(plan) == {"ENG-31": "Done", "ENG-30": "Done"}


def test_gated_parent_is_not_released_when_complete():
    """Given the last sub-issue of a gated story ships,
    When the plan is built,
    Then the parent waits for its gate and its status is untouched."""
    parent = ticket(
        "ENG-40", "gated", "story", state="In Progress",
        children=(child("ENG-41", "Ready for Deployment"),),
    )
    plan = build_plan([ticket("ENG-41", parent="ENG-40")], {"ENG-40": parent}, CFG)
    assert plan.releases == {}
    assert "ENG-40" not in moves(plan)
    assert note(plan, "ENG-40").reason == "gated"


def test_shipped_ticket_with_open_sub_issues_stops_at_deployed():
    """Given an immediate story shipped directly while one of its sub-issues is open,
    When the plan is built,
    Then it is not released, stops at Deployed, and is flagged."""
    t = ticket(
        "ENG-50", "immediate", "story",
        children=(child("ENG-51", "In Progress"),),
    )
    plan = build_plan([t], {}, CFG)
    assert plan.releases == {}
    assert moves(plan) == {"ENG-50": "Deployed"}
    assert note(plan, "ENG-50") == Note("ENG-50", "still has open sub-issues: ENG-51", attention=True)


def test_parent_and_its_last_child_shipped_together_releases_the_parent():
    """Given a story and its only open sub-issue both ship in one deploy,
    When the plan is built,
    Then the sub-issue's move counts and the story is released."""
    parent = ticket(
        "ENG-60", "immediate", "story",
        children=(child("ENG-61", "Ready for Deployment"),),
    )
    sub = ticket("ENG-61", "immediate", parent="ENG-60")
    plan = build_plan([sub, parent], {"ENG-60": parent}, CFG)
    assert ids(plan.releases["astra"]) == ["ENG-60"]
    assert moves(plan) == {"ENG-60": "Done", "ENG-61": "Done"}


def test_releases_and_moves_are_ordered_by_ticket_number():
    """Given tickets found out of numeric order,
    When the plan is built,
    Then release lists and moves follow ticket number."""
    plan = build_plan(
        [ticket("ENG-100", "immediate", "bug"), ticket("ENG-9", "immediate", "bug")], {}, CFG
    )
    assert ids(plan.releases["astra"]) == ["ENG-9", "ENG-100"]
    assert [m.ticket.identifier for m in plan.moves] == ["ENG-9", "ENG-100"]
