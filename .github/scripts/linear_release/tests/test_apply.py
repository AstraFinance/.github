from dataclasses import replace
from datetime import date

import pytest

from linear_release.apply import Context, apply_plan, find_own_releases, release_name
from linear_release.linear_api import LinearError
from linear_release.model import ReleaseRef
from linear_release.rules import build_plan

from .factories import CFG, ticket
from .fakes import STATES, FakeLinear

MUTATIONS = {"create_release", "attach", "detach", "set_state", "comment"}

CTX = Context(
    service="Delta",
    tag="1.192.0",
    previous_tag="1.191.0",
    commit_sha="abc123",
    compare_url="https://github.com/AstraFinance/delta-backend/compare/1.191.0...1.192.0",
    today=date(2026, 11, 13),
    dry_run=False,
)


def _plan(*tickets, own_ids=frozenset(), completed=frozenset()):
    return build_plan(list(tickets), {}, CFG, own_release_ids=own_ids, completed_audiences=completed)


def test_release_name_first_second_third():
    """Given the names already used in a pipeline on the release day,
    When a new release is named,
    Then the first is unsuffixed, the next is .2, then .3, ignoring look-alike names."""
    day = date(2026, 11, 4)
    assert release_name("Astra", day, []) == "Astra 2026.11.04"
    assert release_name("Astra", day, ["Astra 2026.11.04"]) == "Astra 2026.11.04.2"
    assert release_name("Astra", day, ["Astra 2026.11.04", "Astra 2026.11.04.2"]) == "Astra 2026.11.04.3"
    assert release_name("Astra", day, ["Astra 2026.11.04x"]) == "Astra 2026.11.04"


def test_apply_orders_mutations():
    """Given a plan with releases for both audiences, status moves and a flagged ticket,
    When it is applied,
    Then releases are created and filled before statuses move, and owners are told last."""
    fake = FakeLinear()
    plan = _plan(ticket("ENG-1", "immediate", "bug"), ticket("ENG-2", "immediate", "internal"),
                 ticket("ENG-3", "gated", "story"), ticket("ENG-5", "bug"))

    apply_plan(plan, fake, CFG, CTX, own={})

    order = [n for n in fake.names() if n in MUTATIONS]
    phases = ["create_release", "detach", "attach", "set_state", "comment"]
    assert order == sorted(order, key=phases.index)
    assert order.count("create_release") == 2
    assert order.count("set_state") == 4
    assert order.count("comment") == 1


def test_apply_leaves_new_releases_in_progress_for_review():
    """Given a plan with a release,
    When it is applied,
    Then the release is created in its started stage and nothing completes it."""
    fake = FakeLinear()
    outcome = apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, CFG, CTX, own={})

    assert fake.names().count("create_release") == 1
    assert not [n for n in fake.names() if "complete" in n]
    assert outcome.releases[0].released is False
    assert ("set_state", ("uuid-ENG-1", "s-deployed")) in fake.calls


def test_apply_writes_the_marker_first_in_the_description():
    """Given a release is created for a deploy,
    When its fields are sent,
    Then the description starts with the service and tag so a re-run can find it."""
    fake = FakeLinear()
    apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, CFG, CTX, own={})

    _, name, version, sha, description, start = fake.args_of("create_release")[0]
    assert (name, version, sha, start) == ("Astra 2026.11.13", "1.192.0", "abc123", "2026-11-13")
    assert description.startswith("Delta 1.192.0")
    assert CTX.compare_url in description


def test_dry_run_makes_no_mutations():
    """Given a dry run with a releasable bug and a flagged one,
    When the plan is applied,
    Then nothing in Linear changes, and the names and comments that would happen are reported."""
    fake = FakeLinear(existing_names=["Astra 2026.11.13"])
    plan = _plan(ticket("ENG-1", "immediate", "bug"), ticket("ENG-5", "bug"))

    outcome = apply_plan(plan, fake, CFG, replace(CTX, dry_run=True), own={})

    assert not MUTATIONS & set(fake.names())
    assert [r.name for r in outcome.releases] == ["Astra 2026.11.13.2"]
    assert outcome.commented == ("ENG-5",)


def test_rerun_into_a_released_release_sends_new_tickets_to_done():
    """Given a person already moved this deploy's release to Released with ENG-1 in it,
    When a re-run finds a newly qualifying ENG-2,
    Then ENG-2 is attached and moved to Done, ENG-1 is untouched, and nothing is removed."""
    own_ref = ReleaseRef("own-1", "Astra 2026.11.13", "https://linear.app/own-1")
    done_one = ticket("ENG-1", "immediate", "bug", state="Done", state_type="completed",
                      completed_releases=(own_ref,))
    plan = _plan(done_one, ticket("ENG-2", "immediate", "bug"),
                 own_ids=frozenset({"own-1"}), completed=frozenset({"astra"}))
    fake = FakeLinear(attached={"own-1": {"ENG-1": "uuid-ENG-1"}})

    outcome = apply_plan(plan, fake, CFG, CTX, own={"astra": (own_ref, True)})

    assert "create_release" not in fake.names() and "release_issues" not in fake.names()
    assert fake.args_of("attach") == [("uuid-ENG-2", "own-1")]
    assert fake.args_of("set_state") == [("uuid-ENG-2", "s-done")]
    assert outcome.releases[0].released is True
    assert outcome.releases[0].created is False


def test_rerun_into_a_started_release_does_not_attach_tickets_twice():
    """Given an earlier run created this deploy's release and attached ENG-1,
    When the run is repeated with a new ticket,
    Then ENG-1 is not attached again and only the new ticket is."""
    own_ref = ReleaseRef("own-2", "Astra 2026.11.13", "u")
    attached = ticket("ENG-1", "immediate", "bug", release_ids=frozenset({"own-2"}))
    fake = FakeLinear(attached={"own-2": {"ENG-1": "uuid-ENG-1"}})

    apply_plan(_plan(attached, ticket("ENG-2", "immediate", "bug")), fake, CFG, CTX,
               own={"astra": (own_ref, False)})

    assert fake.args_of("attach") == [("uuid-ENG-2", "own-2")]


def test_rerun_removes_tickets_that_no_longer_qualify():
    """Given an open release still holding ENG-3, which was relabeled gated after the first run,
    When the run is repeated,
    Then ENG-3 is removed from the release and ENG-1 stays."""
    own_ref = ReleaseRef("own-3", "Astra 2026.11.13", "u")
    fake = FakeLinear(attached={"own-3": {"ENG-1": "uuid-ENG-1", "ENG-3": "uuid-ENG-3"}})
    plan = _plan(ticket("ENG-1", "immediate", "bug", release_ids=frozenset({"own-3"})),
                 ticket("ENG-3", "gated", "story", release_ids=frozenset({"own-3"})))

    outcome = apply_plan(plan, fake, CFG, CTX, own={"astra": (own_ref, False)})

    assert fake.args_of("detach") == [("uuid-ENG-3", "own-3")]
    assert outcome.releases[0].removed == ("ENG-3",)
    assert outcome.releases[0].tickets == ("ENG-1",)


def test_rerun_empties_a_release_when_nothing_qualifies_any_more():
    """Given an open release whose only ticket was relabeled gated,
    When the run is repeated,
    Then the ticket is removed and the empty release is still reported so a person can cancel it."""
    own_ref = ReleaseRef("own-4", "Astra 2026.11.13", "u")
    fake = FakeLinear(attached={"own-4": {"ENG-3": "uuid-ENG-3"}})

    outcome = apply_plan(_plan(ticket("ENG-3", "gated", "story")), fake, CFG, CTX,
                         own={"astra": (own_ref, False)})

    assert fake.args_of("detach") == [("uuid-ENG-3", "own-4")]
    assert outcome.releases[0].tickets == ()
    assert outcome.releases[0].removed == ("ENG-3",)


def test_rerun_stops_when_a_person_releases_the_release_during_the_run():
    """Given an open release holding ENG-3, now gated, and ENG-2, planned to Deployed, which a person releases
        while the run is going,
    When the run reaches the trim,
    Then the release's stage is read again and the run stops, asking for a re-run, before anything is detached,
        attached or moved, since its tickets were planned to Deployed and Linear will not move them to Done."""
    own_ref = ReleaseRef("own-5", "Astra 2026.11.13", "u")
    fake = FakeLinear(attached={"own-5": {"ENG-1": "uuid-ENG-1", "ENG-3": "uuid-ENG-3"}}, completed={"own-5"})
    plan = _plan(ticket("ENG-1", "immediate", "bug", release_ids=frozenset({"own-5"})),
                 ticket("ENG-2", "immediate", "bug"),
                 ticket("ENG-3", "gated", "story", release_ids=frozenset({"own-5"})))
    assert ("ENG-2", "Deployed") in {(m.ticket.identifier, m.to_state) for m in plan.moves}

    with pytest.raises(LinearError, match="moved to Released while this run was going"):
        apply_plan(plan, fake, CFG, CTX, own={"astra": (own_ref, False)})

    assert fake.args_of("release_completed") == [("own-5",)]
    assert [name for name in fake.names() if name in {"detach", "attach", "set_state"}] == []


def test_flagged_ticket_gets_a_comment_with_what_to_do():
    """Given a bug shipped without a Release Method label,
    When the plan is applied,
    Then its owner gets a comment naming the deploy, the problem and the fix."""
    fake = FakeLinear()
    outcome = apply_plan(_plan(ticket("ENG-5", "bug")), fake, CFG, CTX, own={})

    [(uuid, body)] = fake.args_of("comment")
    assert uuid == "uuid-ENG-5"
    assert "Linear release check · Delta 1.192.0" in body
    assert "no Release Method label" in body
    assert "`immediate` or `gated`" in body
    assert outcome.commented == ("ENG-5",)


def test_flagged_ticket_is_commented_on_once_per_deploy():
    """Given the ticket already carries this deploy's comment from an earlier run,
    When the run is repeated,
    Then no second comment is posted."""
    fake = FakeLinear(comments={"uuid-ENG-5": ["**Linear release check · Delta 1.192.0**\n\nLeft out..."]})
    outcome = apply_plan(_plan(ticket("ENG-5", "bug")), fake, CFG, CTX, own={})

    assert "comment" not in fake.names()
    assert outcome.commented == ()


def test_informational_notes_get_no_comment():
    """Given a deliberately typed task and a gated story,
    When the plan is applied,
    Then nobody is asked to fix anything."""
    fake = FakeLinear()
    apply_plan(_plan(ticket("ENG-4", "immediate", "task"), ticket("ENG-3", "gated", "story")),
               fake, CFG, CTX, own={})

    assert "comment" not in fake.names()


def test_missing_workflow_state_stops_before_any_change():
    """Given the team has no Deployed state, for example after a rename,
    When a plan needing it is applied,
    Then the run stops before creating or changing anything."""
    fake = FakeLinear(states={k: v for k, v in STATES.items() if k != "Deployed"})

    with pytest.raises(LinearError, match="Deployed"):
        apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, CFG, CTX, own={})

    assert not MUTATIONS & set(fake.names())


def test_plan_without_releases_creates_nothing():
    """Given a plan that only moves statuses,
    When it is applied,
    Then no release is created or read."""
    fake = FakeLinear()
    outcome = apply_plan(_plan(ticket("ENG-3", "gated", "story")), fake, CFG, CTX, own={})

    assert outcome.releases == ()
    assert outcome.moved == (("ENG-3", "Deployed"),)
    assert {"create_release", "release_issues"}.isdisjoint(fake.names())


def test_find_own_releases_checks_both_pipelines_with_the_marker():
    """Given a deploy context,
    When this run's own releases are looked up,
    Then each audience's pipeline is searched by tag and marker, and only found ones are returned."""
    own_ref = ReleaseRef("own-3", "Internal 2026.11.13", "u")
    fake = FakeLinear(own={"p-Internal Tooling": (own_ref, True)})

    found = find_own_releases(fake, CFG, CTX)

    assert found == {"internal": (own_ref, True)}
    assert fake.args_of("find_own_release") == [
        ("p-Astra", "1.192.0", "Delta 1.192.0"), ("p-Internal Tooling", "1.192.0", "Delta 1.192.0"),
    ]
