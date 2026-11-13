"""The facts a release decision is made from, and the decision itself."""

from dataclasses import dataclass
from typing import Literal

Audience = Literal["astra", "internal"]


@dataclass(frozen=True)
class ReleaseRef:
    id: str
    name: str
    url: str = ""


@dataclass(frozen=True)
class Child:
    identifier: str
    state: str
    state_type: str


@dataclass(frozen=True)
class Ticket:
    identifier: str
    uuid: str
    title: str
    url: str
    # Lower-cased, so label checks never depend on how a label was capitalized.
    labels: frozenset[str]
    state: str
    state_type: str
    parent: str | None
    children: tuple[Child, ...]
    completed_releases: tuple[ReleaseRef, ...]


@dataclass(frozen=True)
class Move:
    ticket: Ticket
    to_state: str


@dataclass(frozen=True)
class Note:
    """Why a ticket was left out of a release; `attention` marks the ones a person must act on."""

    identifier: str
    reason: str
    attention: bool


@dataclass(frozen=True)
class Plan:
    releases: dict[Audience, tuple[Ticket, ...]]
    moves: tuple[Move, ...]
    notes: tuple[Note, ...]
