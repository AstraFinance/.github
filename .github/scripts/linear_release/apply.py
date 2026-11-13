"""Carry out a release plan in Linear, in an order that is safe to repeat.

Releases are created and filled before any status moves, and completed only
after every released ticket is Done: the pipelines roll open issues into a
new release on completion, so completing earlier would scatter the tickets.
"""

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

from .config import Config
from .linear_api import LinearError, Pipeline
from .model import Audience, Plan, ReleaseRef

AUDIENCES: tuple[Audience, ...] = ("astra", "internal")


@dataclass(frozen=True)
class Context:
    service: str
    tag: str
    previous_tag: str
    commit_sha: str
    compare_url: str
    today: date
    dry_run: bool

    @property
    def marker(self) -> str:
        """How a release made for this deploy is recognized on a re-run."""
        return f"{self.service} {self.tag}"


@dataclass(frozen=True)
class ReleaseResult:
    audience: Audience
    name: str
    url: str
    tickets: tuple[str, ...]
    created: bool
    notes: str | None
    notes_status: str


@dataclass(frozen=True)
class Outcome:
    releases: tuple[ReleaseResult, ...]
    moved: tuple[tuple[str, str], ...]


def release_name(prefix: str, day: date, existing: Iterable[str]) -> str:
    base = f"{prefix} {day:%Y.%m.%d}"
    suffixed = re.compile(rf"^{re.escape(base)}\.(\d+)$")
    taken = 0
    for name in existing:
        if name == base:
            taken = max(taken, 1)
        elif match := suffixed.match(name):
            taken = max(taken, int(match.group(1)))
    return base if taken == 0 else f"{base}.{taken + 1}"


def _pipeline_name(cfg: Config, audience: Audience) -> str:
    return cfg.pipeline_astra if audience == "astra" else cfg.pipeline_internal


def _prefix(cfg: Config, audience: Audience) -> str:
    return cfg.prefix_astra if audience == "astra" else cfg.prefix_internal


def find_own_releases(client, cfg: Config, ctx: Context) -> dict[Audience, tuple[ReleaseRef, bool]]:
    found: dict[Audience, tuple[ReleaseRef, bool]] = {}
    for audience in AUDIENCES:
        pipeline = client.fetch_pipeline(_pipeline_name(cfg, audience))
        own = client.find_own_release(pipeline.id, ctx.tag, ctx.marker)
        if own:
            found[audience] = own
    return found


@dataclass
class _Target:
    audience: Audience
    pipeline: Pipeline
    name: str
    ref: ReleaseRef | None
    created: bool
    was_completed: bool


def apply_plan(
    plan: Plan,
    client,
    cfg: Config,
    ctx: Context,
    own: dict[Audience, tuple[ReleaseRef, bool]],
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    targets: list[_Target] = []
    for audience in AUDIENCES:
        if not plan.releases.get(audience):
            continue
        pipeline = client.fetch_pipeline(_pipeline_name(cfg, audience))
        if audience in own:
            ref, completed = own[audience]
            targets.append(_Target(audience, pipeline, ref.name, ref, False, completed))
            continue
        prefix = _prefix(cfg, audience)
        base = f"{prefix} {ctx.today:%Y.%m.%d}"
        name = release_name(prefix, ctx.today, client.release_names(pipeline.id, base))
        ref = None
        if not ctx.dry_run:
            description = f"{ctx.marker}\n\n{ctx.compare_url}"
            ref = client.create_release(pipeline, name, ctx.tag, ctx.commit_sha, description, ctx.today.isoformat())
        targets.append(_Target(audience, pipeline, name, ref, True, False))

    moved = tuple((m.ticket.identifier, m.to_state) for m in plan.moves)

    if not ctx.dry_run:
        for target in targets:
            for t in plan.releases[target.audience]:
                if target.ref.id not in {r.id for r in t.completed_releases}:
                    client.attach(t.uuid, target.ref.id)

        if plan.moves:
            state_ids = client.state_ids(cfg.team_key)
            for move in plan.moves:
                state_id = state_ids.get(move.to_state)
                if state_id is None:
                    raise LinearError(f"SetState: no workflow state named {move.to_state!r}")
                client.set_state(move.ticket.uuid, state_id)

        for target in targets:
            if not target.was_completed:
                client.complete_release(target.ref.id, target.pipeline.completed_stage_id)

    notes = _await_notes(client, cfg, ctx, targets, sleep)

    results = tuple(
        ReleaseResult(
            audience=target.audience,
            name=target.name,
            url=target.ref.url if target.ref else "",
            tickets=tuple(t.identifier for t in plan.releases[target.audience]),
            created=target.created,
            notes=notes[target.audience][1],
            notes_status=notes[target.audience][0],
        )
        for target in targets
    )
    return Outcome(releases=results, moved=moved)


def _await_notes(client, cfg, ctx, targets, sleep) -> dict[Audience, tuple[str, str | None]]:
    """Wait a bounded time for Linear's generated notes; never fail the run over them."""
    status: dict[Audience, tuple[str, str | None]] = {}
    waiting = []
    for target in targets:
        if ctx.dry_run:
            status[target.audience] = ("dry run", None)
        elif target.was_completed:
            status[target.audience] = ("unchanged", None)
        else:
            waiting.append(target)

    rounds = max(1, cfg.notes_timeout_s // cfg.notes_interval_s)
    for round_number in range(rounds):
        for target in list(waiting):
            state, content = client.notes_status(target.ref.id)
            status[target.audience] = (state, content)
            if state == "completed":
                waiting.remove(target)
        if not waiting:
            break
        if round_number < rounds - 1:
            sleep(cfg.notes_interval_s)
    return status
