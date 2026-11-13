"""Builders that let each test state only the facts its behavior depends on."""

from linear_release.config import Config
from linear_release.model import Child, Note, Plan, ReleaseRef, Ticket

CFG = Config()


def ticket(
    identifier: str,
    *labels: str,
    state: str = "Ready for Deployment",
    state_type: str = "started",
    parent: str | None = None,
    children: tuple[Child, ...] = (),
    completed_releases: tuple[ReleaseRef, ...] = (),
    release_ids: frozenset[str] | None = None,
) -> Ticket:
    return Ticket(
        identifier=identifier,
        uuid=f"uuid-{identifier}",
        title=f"Title of {identifier}",
        url=f"https://linear.app/astra/issue/{identifier}",
        labels=frozenset(label.lower() for label in labels),
        state=state,
        state_type=state_type,
        parent=parent,
        children=children,
        completed_releases=completed_releases,
        release_ids=release_ids if release_ids is not None else frozenset(r.id for r in completed_releases),
    )


def child(identifier: str, state: str, state_type: str = "started") -> Child:
    return Child(identifier=identifier, state=state, state_type=state_type)


def ids(tickets) -> list[str]:
    return [t.identifier for t in tickets]


def moves(plan: Plan) -> dict[str, str]:
    return {move.ticket.identifier: move.to_state for move in plan.moves}


def note(plan: Plan, identifier: str) -> Note:
    matches = [n for n in plan.notes if n.identifier == identifier]
    assert len(matches) == 1, f"expected one note for {identifier}, got {matches}"
    return matches[0]
