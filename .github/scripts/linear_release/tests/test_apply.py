from dataclasses import replace
from datetime import date

from linear_release.apply import Context, apply_plan, find_own_releases, release_name
from linear_release.model import ReleaseRef
from linear_release.rules import build_plan

from .factories import CFG, ticket
from .fakes import FakeLinear

MUTATIONS = {"create_release", "attach", "set_state", "complete_release"}

CTX = Context(
    service="Delta",
    tag="1.192.0",
    previous_tag="1.191.0",
    commit_sha="abc123",
    compare_url="https://github.com/AstraFinance/delta-backend/compare/1.191.0...1.192.0",
    today=date(2026, 11, 13),
    dry_run=False,
)


def _plan(*tickets, own_ids=frozenset()):
    return build_plan(list(tickets), {}, CFG, own_release_ids=own_ids)


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
    """Given a plan with releases for both audiences and status moves,
    When it is applied,
    Then releases are created and filled before statuses move, and completed only after that."""
    fake = FakeLinear()
    plan = _plan(ticket("ENG-1", "immediate", "bug"), ticket("ENG-2", "immediate", "internal"),
                 ticket("ENG-3", "gated", "story"))

    apply_plan(plan, fake, CFG, CTX, own={}, sleep=lambda s: None)

    order = [n for n in fake.names() if n in MUTATIONS | {"notes_status"}]
    phases = ["create_release", "attach", "set_state", "complete_release", "notes_status"]
    assert order == sorted(order, key=phases.index)
    assert order.count("create_release") == 2 and order.count("complete_release") == 2
    assert order.count("set_state") == 3


def test_apply_writes_the_marker_first_in_the_description():
    """Given a release is created for a deploy,
    When its fields are sent,
    Then the description starts with the service and tag so a re-run can find it."""
    fake = FakeLinear()
    result = apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, CFG, CTX, own={}, sleep=lambda s: None)

    create = next(args for name, args in fake.calls if name == "create_release")
    _, name, version, sha, description, start = create
    assert (name, version, sha, start) == ("Astra 2026.11.13", "1.192.0", "abc123", "2026-11-13")
    assert description.startswith("Delta 1.192.0")
    assert CTX.compare_url in description
    assert result.releases[0].notes == "## What changed"


def test_dry_run_makes_no_mutations():
    """Given a dry run,
    When a plan is applied,
    Then nothing in Linear changes and the release names that would be used are still reported."""
    fake = FakeLinear(existing_names=["Astra 2026.11.13"])
    plan = _plan(ticket("ENG-1", "immediate", "bug"))

    outcome = apply_plan(plan, fake, CFG, replace(CTX, dry_run=True), own={}, sleep=lambda s: None)

    assert not MUTATIONS & set(fake.names())
    assert [r.name for r in outcome.releases] == ["Astra 2026.11.13.2"]
    assert outcome.releases[0].notes_status == "dry run"


def test_rerun_reuses_existing_release_and_skips_done_work():
    """Given a re-run after the release was completed with ENG-1 already in it and Done,
    When the plan now also holds ENG-2,
    Then no release is created or completed again, ENG-1 is untouched and only ENG-2 is attached and moved."""
    own_ref = ReleaseRef("own-1", "Astra 2026.11.13", "https://linear.app/own-1")
    done_one = ticket("ENG-1", "immediate", "bug", state="Done", state_type="completed",
                      completed_releases=(own_ref,))
    new_two = ticket("ENG-2", "immediate", "bug")
    plan = _plan(done_one, new_two, own_ids=frozenset({"own-1"}))
    fake = FakeLinear()

    outcome = apply_plan(plan, fake, CFG, CTX, own={"astra": (own_ref, True)}, sleep=lambda s: None)

    assert "create_release" not in fake.names() and "complete_release" not in fake.names()
    assert [args for name, args in fake.calls if name == "attach"] == [("uuid-ENG-2", "own-1")]
    assert [args for name, args in fake.calls if name == "set_state"] == [("uuid-ENG-2", "s-done")]
    assert outcome.releases[0].name == "Astra 2026.11.13"
    assert outcome.releases[0].created is False
    assert outcome.releases[0].notes_status == "unchanged"


def test_rerun_completes_a_release_left_open_by_a_failed_run():
    """Given an earlier run created this deploy's release but failed before completing it,
    When the run is repeated,
    Then the existing release is completed and its notes are awaited."""
    own_ref = ReleaseRef("own-2", "Astra 2026.11.13", "u")
    fake = FakeLinear()

    outcome = apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, CFG, CTX,
                         own={"astra": (own_ref, False)}, sleep=lambda s: None)

    assert "create_release" not in fake.names()
    assert ("complete_release", ("own-2", "done-Astra")) in fake.calls
    assert outcome.releases[0].notes_status == "completed"


def test_notes_timeout_leaves_pending_message():
    """Given Linear never finishes generating notes,
    When the plan is applied with a 30 second budget polled every 10 seconds,
    Then it polls three times and reports the notes as pending."""
    fake = FakeLinear(notes=("pending", None))
    cfg = replace(CFG, notes_timeout_s=30, notes_interval_s=10)
    slept = []

    outcome = apply_plan(_plan(ticket("ENG-1", "immediate", "bug")), fake, cfg, CTX, own={}, sleep=slept.append)

    assert fake.names().count("notes_status") == 3
    assert slept == [10, 10]
    assert outcome.releases[0].notes_status == "pending"
    assert outcome.releases[0].notes is None


def test_plan_without_releases_creates_nothing():
    """Given a plan that only moves statuses,
    When it is applied,
    Then no release is created, completed or polled."""
    fake = FakeLinear()
    outcome = apply_plan(_plan(ticket("ENG-3", "gated", "story")), fake, CFG, CTX, own={}, sleep=lambda s: None)

    assert outcome.releases == ()
    assert outcome.moved == (("ENG-3", "Deployed"),)
    assert {"create_release", "complete_release", "notes_status"}.isdisjoint(fake.names())


def test_find_own_releases_checks_both_pipelines_with_the_marker():
    """Given a deploy context,
    When this run's own releases are looked up,
    Then each audience's pipeline is searched by tag and marker, and only found ones are returned."""
    own_ref = ReleaseRef("own-3", "Internal 2026.11.13", "u")
    fake = FakeLinear(own={"p-Internal Tooling": (own_ref, True)})

    found = find_own_releases(fake, CFG, CTX)

    assert found == {"internal": (own_ref, True)}
    searched = [args for name, args in fake.calls if name == "find_own_release"]
    assert searched == [("p-Astra", "1.192.0", "Delta 1.192.0"), ("p-Internal Tooling", "1.192.0", "Delta 1.192.0")]
