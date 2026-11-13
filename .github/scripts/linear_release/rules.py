"""Which shipped tickets form a release, and where each ticket's status goes.

Everything here is decided from labels and states alone, so the whole policy
can be read and tested without Linear or git.
"""

from typing import Mapping, Sequence

from .config import Config
from .model import Audience, Move, Note, Plan, ReleaseRef, Ticket

# A sub-issue in one of these state types no longer holds its parent back.
_FINISHED_TYPES = frozenset({"completed", "canceled", "duplicate"})


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

    def method_required(self, t: Ticket) -> bool:
        return any(self.has(t, kind) for kind in self.cfg.method_required_types)

    def internal(self, t: Ticket) -> bool:
        return self.has(t, self.cfg.label_internal)

    def typed(self, t: Ticket) -> bool:
        return self.internal(t) or any(self.has(t, kind) for kind in self.cfg.released_types)

    def other_release(self, t: Ticket) -> ReleaseRef | None:
        return next((r for r in t.completed_releases if r.id not in self.own), None)

    def movable(self, t: Ticket) -> bool:
        return t.state in (self.cfg.state_ready, self.cfg.state_deployed, self.cfg.state_done)

    def label_target(self, t: Ticket) -> str:
        """Where the labels alone would send a shipped ticket."""
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
) -> Plan:
    rules = _Rules(cfg, own_release_ids)
    tickets = sorted({t.identifier: t for t in shipped}.values(), key=lambda t: _number(t.identifier))
    shipped_ids = {t.identifier for t in tickets}

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
            notes.append(Note(t.identifier, f"{t.state}, not {cfg.state_ready}", True))
            continue

        kids = open_children(t)
        target = rules.label_target(t)
        held_by_kids = bool(kids) and target == cfg.state_done
        if held_by_kids:
            target = cfg.state_deployed
        if t.state != cfg.state_done and target != t.state:
            moves.append(Move(t, target))

        # Reasons that need a person come first, so a flag is never hidden
        # behind an informational reason such as being a sub-issue.
        reason: tuple[str, bool] | None = None
        if rules.advance(t):
            reason = ("advance-notice-required: release it through its planned release", True)
        elif not rules.has_method(t) and rules.method_required(t):
            reason = ("no Release Method label", True)
        elif held_by_kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", True)
        elif t.parent:
            reason = (f"sub-issue of {t.parent}", False)
        elif rules.gated(t):
            reason = ("gated", False)
        elif not rules.has_method(t):
            reason = ("no Release Method label", False)
        elif kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", False)
        elif not rules.typed(t):
            reason = ("no story or bug label", False)

        if reason:
            notes.append(Note(t.identifier, *reason))
        else:
            releases.setdefault(rules.audience(t), []).append(t)

    for pid in sorted(parents_to_check - shipped_ids, key=_number):
        p = parents.get(pid)
        if p is None:
            notes.append(Note(pid, "parent could not be read from Linear", True))
            continue
        other = rules.other_release(p)
        kids = open_children(p)
        reason = None
        if other:
            reason = (f"already in {other.name}", False)
        elif p.state_type in ("canceled", "duplicate"):
            # Work shipped under a story nobody intends to deliver: announcing
            # it would tell clients about something that was called off.
            reason = (f"{p.state}, so not released", True)
        elif p.parent:
            reason = ("is itself a sub-issue", False)
        elif kids:
            reason = (f"still has open sub-issues: {', '.join(kids)}", False)
        elif rules.advance(p):
            reason = ("advance-notice-required: release it through its planned release", True)
        elif rules.gated(p):
            reason = ("gated", False)
        elif not rules.has_method(p):
            reason = ("no Release Method label", rules.method_required(p))
        elif not rules.typed(p):
            reason = ("no story or bug label", False)

        if reason:
            notes.append(Note(pid, *reason))
            continue
        releases.setdefault(rules.audience(p), []).append(p)
        if p.state != cfg.state_done:
            moves.append(Move(p, cfg.state_done))

    def by_number(items):
        return tuple(sorted(items, key=lambda t: _number(t.identifier)))

    return Plan(
        releases={audience: by_number(ts) for audience, ts in releases.items()},
        moves=tuple(sorted(moves, key=lambda m: _number(m.ticket.identifier))),
        notes=tuple(notes),
    )
