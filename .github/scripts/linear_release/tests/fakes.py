"""In-memory stand-ins shared by tests that exercise code talking to Linear."""

from linear_release.linear_api import Pipeline
from linear_release.model import ReleaseRef

STATES = {"Ready for Deployment": "s-ready", "Deployed": "s-deployed", "Done": "s-done"}


class FakeLinear:
    """An in-memory stand-in for LinearClient that logs every call in order."""

    def __init__(
        self, existing_names=(), own=None, tickets=None, attached=None, comments=None, states=None,
        completed=(),
    ):
        self.tickets = {t.identifier: t for t in (tickets or ())}
        self.calls = []
        self.existing_names = list(existing_names)
        self.own = own or {}
        # release id -> {identifier: uuid} already attached to that release
        self.attached = attached or {}
        # ticket uuid -> comment bodies already on that ticket
        self.comments = comments or {}
        self.states = STATES if states is None else states
        # release ids a person has moved to Released by the time the run re-reads them
        self.completed = set(completed)
        self.created = 0

    def _log(self, name, *args):
        self.calls.append((name, args))

    def fetch_tickets(self, team_key, identifiers):
        self._log("fetch_tickets", team_key, tuple(identifiers))
        return {i: self.tickets[i] for i in identifiers if i in self.tickets}

    def state_ids(self, team_key):
        self._log("state_ids", team_key)
        return dict(self.states)

    def fetch_pipeline(self, name):
        self._log("fetch_pipeline", name)
        return Pipeline(f"p-{name}", name, f"start-{name}", f"done-{name}")

    def find_own_release(self, pipeline_id, version, marker):
        self._log("find_own_release", pipeline_id, version, marker)
        return self.own.get(pipeline_id)

    def release_names(self, pipeline_id, base_name):
        self._log("release_names", pipeline_id, base_name)
        return [n for n in self.existing_names if n.startswith(base_name)]

    def create_release(self, pipeline, name, version, commit_sha, description, start_date):
        self._log("create_release", pipeline.id, name, version, commit_sha, description, start_date)
        self.created += 1
        return ReleaseRef(f"rel-{self.created}", name, f"https://linear.app/rel-{self.created}")

    def release_issues(self, release_id):
        self._log("release_issues", release_id)
        return dict(self.attached.get(release_id, {}))

    def attach(self, issue_uuid, release_id):
        self._log("attach", issue_uuid, release_id)

    def release_completed(self, release_id):
        self._log("release_completed", release_id)
        return release_id in self.completed

    def detach(self, issue_uuid, release_id):
        self._log("detach", issue_uuid, release_id)

    def set_state(self, issue_uuid, state_id):
        self._log("set_state", issue_uuid, state_id)

    def has_comment(self, issue_uuid, text):
        self._log("has_comment", issue_uuid, text)
        return any(text in body for body in self.comments.get(issue_uuid, ()))

    def comment(self, issue_uuid, body):
        self._log("comment", issue_uuid, body)

    def names(self):
        return [name for name, _ in self.calls]

    def args_of(self, name):
        return [args for call, args in self.calls if call == name]
