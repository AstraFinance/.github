"""The few Linear GraphQL operations a release run needs.

Stdlib only, so the workflow runs on a bare runner with nothing to install.
Errors carry the operation name and Linear's own message, never the request,
so the API key cannot leak into logs.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from .model import Child, ReleaseRef, Ticket

API_URL = "https://api.linear.app/graphql"
_CHUNK = 100

FETCH_TICKETS_GQL = """
query FetchTickets($teamKey: String!, $numbers: [Float!]!) {
  issues(filter: {team: {key: {eq: $teamKey}}, number: {in: $numbers}}, first: 100) {
    nodes {
      id
      identifier
      title
      url
      state { name type }
      parent { identifier }
      labels(first: 50) { nodes { name } }
      children(first: 100) { nodes { identifier state { name type } } }
      releases(first: 50, includeArchived: true) { nodes { id name url stage { type } } }
    }
  }
}
"""

STATE_IDS_GQL = """
query StateIds($teamKey: String!) {
  workflowStates(filter: {team: {key: {eq: $teamKey}}}, first: 100) {
    nodes { id name }
  }
}
"""

FETCH_PIPELINE_GQL = """
query FetchPipeline($name: String!) {
  releasePipelines(filter: {name: {eq: $name}}, first: 5) {
    nodes { id name stages(first: 20) { nodes { id type } } }
  }
}
"""

FIND_OWN_RELEASE_GQL = """
query FindOwnRelease($pipelineId: ID!, $version: String!) {
  releases(filter: {pipeline: {id: {eq: $pipelineId}}, version: {eq: $version}}, first: 50, includeArchived: true) {
    nodes { id name url description stage { type } }
  }
}
"""

RELEASE_NAMES_GQL = """
query ReleaseNames($pipelineId: ID!, $prefix: String!) {
  releases(filter: {pipeline: {id: {eq: $pipelineId}}, name: {startsWith: $prefix}}, first: 100) {
    nodes { name }
  }
}
"""

CREATE_RELEASE_GQL = """
mutation CreateRelease($input: ReleaseCreateInput!) {
  releaseCreate(input: $input) { success release { id name url } }
}
"""

UPDATE_ISSUE_GQL = """
mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}
"""

COMPLETE_RELEASE_GQL = """
mutation CompleteRelease($id: String!, $input: ReleaseUpdateInput!) {
  releaseUpdate(id: $id, input: $input) { success }
}
"""

NOTES_STATUS_GQL = """
query NotesStatus($id: String!) {
  release(id: $id) { releaseNotes { generationStatus documentContent { content } } }
}
"""


class LinearError(Exception):
    pass


@dataclass(frozen=True)
class Pipeline:
    id: str
    name: str
    started_stage_id: str
    completed_stage_id: str


Post = Callable[[str, dict[str, str]], dict]


def _urllib_post(body: str, headers: dict[str, str], attempts: int = 3) -> dict:
    """POST with retries on rate limits and server errors, which are transient."""
    for attempt in range(attempts):
        request = urllib.request.Request(API_URL, data=body.encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            if retryable and attempt < attempts - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            try:
                return json.loads(error.read())
            except ValueError:
                raise LinearError(f"Linear returned HTTP {error.code}") from None
        except urllib.error.URLError as error:
            if attempt < attempts - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise LinearError(f"could not reach Linear: {error.reason}") from None
    raise LinearError("could not reach Linear")


def _number(identifier: str) -> float:
    return float(identifier.rsplit("-", 1)[1])


class LinearClient:
    def __init__(self, api_key: str, post: Post | None = None):
        self._api_key = api_key
        self._post = post or _urllib_post

    def _run(self, operation: str, document: str, variables: dict) -> dict:
        body = json.dumps({"query": document, "variables": variables})
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "User-Agent": "linear-release-workflow",
        }
        response = self._post(body, headers)
        errors = response.get("errors")
        if errors:
            messages = "; ".join(str(e.get("message", "unknown error")) for e in errors)
            raise LinearError(f"{operation}: {messages}")
        if "data" not in response or response["data"] is None:
            raise LinearError(f"{operation}: empty response")
        return response["data"]

    def _mutate(self, operation: str, document: str, variables: dict, field: str) -> dict:
        payload = self._run(operation, document, variables)[field]
        if not payload.get("success"):
            raise LinearError(f"{operation}: Linear reported failure")
        return payload

    def fetch_tickets(self, team_key: str, identifiers: Sequence[str]) -> dict[str, Ticket]:
        wanted = {i.upper() for i in identifiers}
        numbers = sorted({_number(i) for i in wanted})
        found: dict[str, Ticket] = {}
        for start in range(0, len(numbers), _CHUNK):
            chunk = numbers[start:start + _CHUNK]
            data = self._run("FetchTickets", FETCH_TICKETS_GQL, {"teamKey": team_key, "numbers": chunk})
            for node in data["issues"]["nodes"]:
                if node["identifier"] in wanted:
                    found[node["identifier"]] = _ticket(node)
        return found

    def state_ids(self, team_key: str) -> dict[str, str]:
        data = self._run("StateIds", STATE_IDS_GQL, {"teamKey": team_key})
        return {node["name"]: node["id"] for node in data["workflowStates"]["nodes"]}

    def fetch_pipeline(self, name: str) -> Pipeline:
        data = self._run("FetchPipeline", FETCH_PIPELINE_GQL, {"name": name})
        nodes = data["releasePipelines"]["nodes"]
        if not nodes:
            raise LinearError(f"FetchPipeline: no release pipeline named {name!r}")
        node = nodes[0]
        stages = node["stages"]["nodes"]
        started = next((s["id"] for s in stages if s["type"] == "started"), None)
        completed = next((s["id"] for s in stages if s["type"] == "completed"), None)
        if not started or not completed:
            raise LinearError(f"FetchPipeline: pipeline {name!r} needs a started and a completed stage")
        return Pipeline(node["id"], node["name"], started, completed)

    def find_own_release(self, pipeline_id: str, version: str, marker: str) -> tuple[ReleaseRef, bool] | None:
        """This deploy's release in the pipeline, if an earlier run made it.

        Linear keeps a version unique within a pipeline, archived releases
        included, so a release holding the version that this workflow did not
        make would make every create fail; the run stops here instead, before
        anything changes.
        """
        data = self._run("FindOwnRelease", FIND_OWN_RELEASE_GQL, {"pipelineId": pipeline_id, "version": version})
        nodes = data["releases"]["nodes"]
        for node in nodes:
            if (node.get("description") or "").startswith(marker):
                ref = ReleaseRef(node["id"], node["name"], node["url"])
                return ref, node["stage"]["type"] == "completed"
        if nodes:
            raise LinearError(
                f"FindOwnRelease: release {nodes[0]['name']!r} already uses version {version!r} in this "
                f"pipeline but was not made by this workflow for {marker!r}. Change that release's "
                f"version in Linear, then run again."
            )
        return None

    def release_names(self, pipeline_id: str, base_name: str) -> list[str]:
        data = self._run("ReleaseNames", RELEASE_NAMES_GQL, {"pipelineId": pipeline_id, "prefix": base_name})
        return [node["name"] for node in data["releases"]["nodes"]]

    def create_release(
        self,
        pipeline: Pipeline,
        name: str,
        version: str,
        commit_sha: str,
        description: str,
        start_date: str,
    ) -> ReleaseRef:
        payload = self._mutate(
            "CreateRelease",
            CREATE_RELEASE_GQL,
            {"input": {
                "pipelineId": pipeline.id,
                "stageId": pipeline.started_stage_id,
                "name": name,
                "version": version,
                "commitSha": commit_sha,
                "description": description,
                "startDate": start_date,
            }},
            "releaseCreate",
        )
        release = payload["release"]
        return ReleaseRef(release["id"], release["name"], release["url"])

    def attach(self, issue_uuid: str, release_id: str) -> None:
        self._mutate(
            "AttachToRelease", UPDATE_ISSUE_GQL,
            {"id": issue_uuid, "input": {"addedReleaseIds": [release_id]}}, "issueUpdate",
        )

    def set_state(self, issue_uuid: str, state_id: str) -> None:
        self._mutate(
            "SetState", UPDATE_ISSUE_GQL, {"id": issue_uuid, "input": {"stateId": state_id}}, "issueUpdate",
        )

    def complete_release(self, release_id: str, completed_stage_id: str) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        self._mutate(
            "CompleteRelease", COMPLETE_RELEASE_GQL,
            {"id": release_id, "input": {"stageId": completed_stage_id, "completedAt": completed_at}},
            "releaseUpdate",
        )

    def notes_status(self, release_id: str) -> tuple[str, str | None]:
        data = self._run("NotesStatus", NOTES_STATUS_GQL, {"id": release_id})
        notes = data["release"]["releaseNotes"]
        for note in notes:
            content = (note.get("documentContent") or {}).get("content")
            if note.get("generationStatus") == "completed" and content:
                return "completed", content
        if notes:
            return "pending", None
        return "missing", None


def _ticket(node: dict) -> Ticket:
    return Ticket(
        identifier=node["identifier"],
        uuid=node["id"],
        title=node["title"],
        url=node["url"],
        labels=frozenset(label["name"].lower() for label in node["labels"]["nodes"]),
        state=node["state"]["name"],
        state_type=node["state"]["type"],
        parent=(node.get("parent") or {}).get("identifier"),
        children=tuple(
            Child(c["identifier"], c["state"]["name"], c["state"]["type"]) for c in node["children"]["nodes"]
        ),
        completed_releases=tuple(
            ReleaseRef(r["id"], r["name"], r["url"])
            for r in node["releases"]["nodes"]
            if r["stage"]["type"] == "completed"
        ),
        release_ids=frozenset(r["id"] for r in node["releases"]["nodes"]),
    )
