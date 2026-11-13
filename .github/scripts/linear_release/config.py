"""Names the release rules depend on.

Labels, workflow states and pipelines are matched by name, so they live in one
place where a rename in Linear needs a one-line change here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    team_key: str = "ENG"

    label_immediate: str = "immediate"
    label_gated: str = "gated"
    label_internal: str = "internal"
    label_advance_notice: str = "advance-notice-required"
    # Type labels whose tickets appear in an Astra release.
    released_types: tuple[str, ...] = ("story", "bug")
    # Ticket kinds that must carry a Release Method label before they ship.
    method_required_types: tuple[str, ...] = ("story", "bug", "tech-debt", "internal")

    state_ready: str = "Ready for Deployment"
    state_deployed: str = "Deployed"
    state_done: str = "Done"

    pipeline_astra: str = "Astra"
    pipeline_internal: str = "Internal Tooling"
    prefix_astra: str = "Astra"
    prefix_internal: str = "Internal"

    # Release names carry the availability date as the team sees it.
    timezone: str = "America/New_York"

    notes_timeout_s: int = 180
    notes_interval_s: int = 10
