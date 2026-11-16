"""Which shipped tickets form a release, and where each ticket's status goes.

Everything here is decided from labels and states alone, so the whole policy
can be read and tested without Linear or git.

Tickets in a release stop at Deployed: a person reviews the release in Linear
and moves it to Released, and Linear's own automation then moves them to Done.
"""

from typing import Mapping, Sequence

from .config import Config
from .model import Audience, Move, Note, Plan, ReleaseRef, Ticket

# A sub-issue in one of these state types no longer holds its parent back.
_FINISHED_TYPES = frozenset({"completed", "canceled", "duplicate"})

_RERUN = "then re-run the Linear release workflow for this tag."
HINT_METHOD = f"Add `immediate` or `gated`, {_RERUN}"
HINT_TYPE = f"Add `story` or `bug` if clients should hear about it, or `task` or `tech-debt` if not, {_RERUN}"
HINT_TYPE_AND_METHOD = (
    f"Add a type (`story`, `bug`, `task` or `tech-debt`) and a Release Method (`immediate` or `gated`), {_RERUN}"
)
HINT_NOT_READY = f"If all of its work has merged, move it to Ready for Deployment, {_RERUN}"
HINT_ADVANCE = "Release it through its planned release."
HINT_KIDS = "It stays at Deployed until its open sub-issues ship."
HINT_CANCELED = "Its sub-issues shipped while it is canceled. Reopen it if the work is going ahead."


def _number(identifier: str) -> int:
    return int(identifier.rsplit("-", 1)[1])


class _Rules:
    def __init__(self, cfg: Config, own_release_ids: frozenset[str]):
        self.cfg = cfg
        self.own = own_release_ids

    def has(self, t: Ticket, label: str) -> bool:
        return label.lower() in t.labels

    def gated(self, t: Ticket) -> bool:
        return self.has(t, self.cfg.label_gated)

    def immediate(self, t: Ticket) -> bool:
        return self.has(t, self.cfg.label_immediate)

    def advance(self, t: Ticket) -> bool:
        return self.has(t, self.cfg.label_advance_notice)

    def has_method(self, t: Ticket) -> bool:
        return self.immediate(t) or self.gated(t)

    def has_type(self, t: Ticket) -> bool:
        return any(self.has(t, kind) for kind in self.cfg.type_labels)

    def method_required(self, t: Ticket) -> bool:
        """Whether shipping this ticket without a Release Method needs a person.

        Sub-issues are usually filed without labels, but a top-level ticket with
        no type at all could be a story whose gate nobody recorded.
        """
        if any(self.has(t, kind) for kind in self.cfg.method_required_types):
            return True
        return t.parent is None and not self.has_type(t)

    def internal(self, t: Ticket) -> bool:
        return self.has(t, self.cfg.label_internal)

    def typed(self, t: Ticket) -> bool:
        return self.internal(t) or any(self.has(t, kind) for kind in self.cfg.released_types)

    def missing_type(self, t: Ticket) -> bool:
        """An immediate top-level ticket with no type label may be a story nobody labeled."""
        return t.parent is None and self.immediate(t) and not self.internal(t) and not self.has_type(t)

    def missing_method_reason(self, t: Ticket) -> tuple[str, bool, str]:
        if t.parent is None and not self.has_type(t):
            return "no type or Release Method label", True, HINT_TYPE_AND_METHOD
        if self.method_required(t):
            return "no Release Method label", True, HINT_METHOD
        return "no Release Method label", False, ""

    def other_release(self, t: Ticket) -> ReleaseRef | None:
        return next((r for r in t.completed_releases if r.id not in self.own), None)

    def movable(self, t: Ticket) -> bool:
        return t.state in (self.cfg.state_ready, self.cfg.state_deployed, self.cfg.state_done)

    def label_target(self, t: Ticket) -> str:
        """Where the labels alone would send a shipped ticket that is not released."""
        if self.gated(t) or self.advance(t):
            return self.cfg.state_deployed
        if not self.has_method(t) and self.method_required(t):
            return self.cfg.state_deployed
        return self.cfg.state_done

    def audience(self, t: Ticket) -> Audience:
        return "internal" if self.internal(t) else "astra"


def build_plan(
    shipped: Sequence[Ticket],
    parents: Mapping[str, Ticket],
    cfg: Config,
    own_release_ids: frozenset[str] = frozenset(),
    completed_audiences: frozenset[Audience] = frozenset(),
) -> Plan:
    """Decide releases, status moves and notes for one deploy.

    `completed_audiences` names this deploy's releases a person has already
    moved to Released: tickets added to those go straight to Done, because no
    later release event will move them.
    """
    rules = _Rules(cfg, own_release_ids)
    tickets = sorted({t.identifier: t for t in shipped}.values(), key=lambda t: _number(t.identifier))
    shipped_ids = {t.identifier for t in tickets}

    def release_target(audience: Audience) -> str:
        return cfg.state_done if audience in completed_audiences else cfg.state_deployed

    # Each shipped ticket's state after this run, from its labels alone. Parents
    # and siblings judge "still open" against these, so a sub-issue shipping in
    # this deploy already counts as finished.
    final: dict[str, tuple[str, str]] = {}
    for t in tickets:
        if rules.other_release(t) or not rules.movable(t) or t.state == cfg.state_done:
            final[t.identifier] = (t.state, t.state_type)
        else:
            final[t.identifier] = (rules.label_target(t), "")

    def open_children(t: Ticket) -> list[str]:
        still_open = []
        for c in t.children:
            state, state_type = final.get(c.identifier, (c.state, c.state_type))
            finished = state in (cfg.state_deployed, cfg.state_done) or state_type in _FINISHED_TYPES
            if not finished:
                still_open.append(c.identifier)
        return still_open

    releases: dict[Audience, list[Ticket]] = {}
    moves: list[Move] = []
    notes: list[Note] = []
    parents_to_check: set[str] = set()

    for t in tickets:
        if t.parent:
            parents_to_check.add(t.parent)

        other = rules.other_release(t)
        if other:
            notes.append(Note(t.identifier, f"already in {other.name}", False))
            continue
        if not rules.movable(t):
            notes.append(Note(t.identifier, f"{t.state}, not {cfg.state_ready}", True, HINT_NOT_READY, t.uuid))
            continue

        kids = open_children(t)
        target = rules.label_target(t)
        held_by_kids = bool(kids) and target == cfg.state_done
        if held_by_kids:
            target = cfg.state_deployed

        # Reasons that need a person come first, so a flag is never hidden
        # behind an informational reason such as being a sub-issue.
        reason: tuple[str, bool, str] | None = None
        if rules.advance(t):
            reason = ("advance-notice-required", True, HINT_ADVANCE)
        elif not rules.has_method(t) and rules.method_required(t):
            reason = rules.missing_method_reason(t)
        elif held_by_kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", True, HINT_KIDS)
        elif rules.missing_type(t):
            reason = ("no type label", True, HINT_TYPE)
        elif t.parent:
            reason = (f"sub-issue of {t.parent}", False, "")
        elif rules.gated(t):
            reason = ("gated", False, "")
        elif not rules.has_method(t):
            reason = rules.missing_method_reason(t)
        elif kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", False, "")
        elif not rules.typed(t):
            reason = ("not a story or bug", False, "")

        if reason:
            notes.append(Note(t.identifier, *reason, t.uuid))
        else:
            audience = rules.audience(t)
            releases.setdefault(audience, []).append(t)
            target = release_target(audience)
        if t.state != cfg.state_done and target != t.state:
            moves.append(Move(t, target))

    for pid in sorted(parents_to_check - shipped_ids, key=_number):
        p = parents.get(pid)
        if p is None:
            notes.append(Note(pid, "parent could not be read from Linear", True))
            continue
        other = rules.other_release(p)
        kids = open_children(p)
        reason = None
        if other:
            reason = (f"already in {other.name}", False, "")
        elif p.state_type in ("canceled", "duplicate"):
            # Work shipped under a story nobody intends to deliver: announcing
            # it would tell clients about something that was called off.
            reason = (f"{p.state}, so not released", True, HINT_CANCELED)
        elif p.parent:
            reason = ("is itself a sub-issue", False, "")
        elif kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", False, "")
        elif rules.advance(p):
            reason = ("advance-notice-required", True, HINT_ADVANCE)
        elif rules.gated(p):
            reason = ("gated", False, "")
        elif not rules.has_method(p):
            reason = rules.missing_method_reason(p)
        elif rules.missing_type(p):
            reason = ("no type label", True, HINT_TYPE)
        elif not rules.typed(p):
            reason = ("not a story or bug", False, "")

        if reason:
            notes.append(Note(pid, *reason, p.uuid))
            continue
        audience = rules.audience(p)
        releases.setdefault(audience, []).append(p)
        target = release_target(audience)
        if p.state not in (cfg.state_done, target):
            moves.append(Move(p, target))

    def by_number(items):
        return tuple(sorted(items, key=lambda t: _number(t.identifier)))

    return Plan(
        releases={audience: by_number(ts) for audience, ts in releases.items()},
        moves=tuple(sorted(moves, key=lambda m: _number(m.ticket.identifier))),
        notes=tuple(notes),
    )
