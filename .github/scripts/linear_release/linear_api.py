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
from typing import Callable, Sequence

from .model import Child, ReleaseRef, Ticket

API_URL = "https://api.linear.app/graphql"

# Linear rejects any query above 10,000 complexity points, and scores a
# connection by its declared `first:` times the cost of each node, whatever
# comes back. So tickets are fetched a few at a time with small nested bounds,
# and a nested list longer than its bound is read to the end page by page.
_CHUNK = 25
_NESTED = 25
_PAGE = 100

_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_LABEL_FIELDS = "nodes { name }"
_CHILD_FIELDS = "nodes { identifier state { name type } }"
_RELEASE_FIELDS = "nodes { id name url stage { type } }"

FETCH_TICKETS_GQL = f"""
query FetchTickets($teamKey: String!, $numbers: [Float!]!) {{
  issues(filter: {{team: {{key: {{eq: $teamKey}}}}, number: {{in: $numbers}}}}, first: {_CHUNK}) {{
    nodes {{
      id
      identifier
      title
      url
      state {{ name type }}
      parent {{ identifier }}
      labels(first: {_NESTED}) {{ {_LABEL_FIELDS} {_PAGE_INFO} }}
      children(first: {_NESTED}) {{ {_CHILD_FIELDS} {_PAGE_INFO} }}
      releases(first: {_NESTED}, includeArchived: true) {{ {_RELEASE_FIELDS} {_PAGE_INFO} }}
    }}
  }}
}}
"""

# The rest of one ticket's nested list, after the page FetchTickets returned.
ISSUE_PAGE_GQL = {
    "labels": f"""
query IssueLabels($id: String!, $after: String) {{
  issue(id: $id) {{ labels(first: {_PAGE}, after: $after) {{ {_LABEL_FIELDS} {_PAGE_INFO} }} }}
}}
""",
    "children": f"""
query IssueChildren($id: String!, $after: String) {{
  issue(id: $id) {{ children(first: {_PAGE}, after: $after) {{ {_CHILD_FIELDS} {_PAGE_INFO} }} }}
}}
""",
    "releases": f"""
query IssueReleases($id: String!, $after: String) {{
  issue(id: $id) {{ releases(first: {_PAGE}, after: $after, includeArchived: true) {{ {_RELEASE_FIELDS} {_PAGE_INFO} }} }}
}}
""",
}

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

RELEASE_ISSUES_GQL = f"""
query ReleaseIssues($id: String!, $after: String) {{
  release(id: $id) {{ issues(first: 250, after: $after) {{ nodes {{ id identifier }} {_PAGE_INFO} }} }}
}}
"""

RELEASE_STAGE_GQL = """
query ReleaseStage($id: String!) {
  release(id: $id) { stage { type } }
}
"""

ISSUE_COMMENTS_GQL = """
query IssueComments($id: String!) {
  issue(id: $id) { comments(first: 100) { nodes { body } } }
}
"""

CREATE_COMMENT_GQL = """
mutation CreateComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
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
                    for field in ISSUE_PAGE_GQL:
                        self._read_to_end(node, field)
                    found[node["identifier"]] = _ticket(node)
        return found

    def _read_to_end(self, node: dict, field: str) -> None:
        """Append the rest of a ticket's nested list, so no label, child or release is decided on unseen."""
        connection = node[field]
        page_info = connection["pageInfo"]
        while page_info["hasNextPage"]:
            data = self._run(
                f"FetchTickets.{field}", ISSUE_PAGE_GQL[field], {"id": node["id"], "after": page_info["endCursor"]},
            )
            page = data["issue"][field]
            connection["nodes"].extend(page["nodes"])
            page_info = page["pageInfo"]

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

    def release_issues(self, release_id: str) -> dict[str, str]:
        """Every ticket attached to a release, by identifier, with its id, read page by page."""
        attached: dict[str, str] = {}
        after = None
        while True:
            data = self._run("ReleaseIssues", RELEASE_ISSUES_GQL, {"id": release_id, "after": after})
            issues = data["release"]["issues"]
            attached.update({node["identifier"]: node["id"] for node in issues["nodes"]})
            if not issues["pageInfo"]["hasNextPage"]:
                return attached
            after = issues["pageInfo"]["endCursor"]

    def release_completed(self, release_id: str) -> bool:
        """Whether a person has moved the release to Released, read now rather than earlier in the run."""
        data = self._run("ReleaseStage", RELEASE_STAGE_GQL, {"id": release_id})
        return data["release"]["stage"]["type"] == "completed"

    def detach(self, issue_uuid: str, release_id: str) -> None:
        self._mutate(
            "DetachFromRelease", UPDATE_ISSUE_GQL,
            {"id": issue_uuid, "input": {"removedReleaseIds": [release_id]}}, "issueUpdate",
        )

    def has_comment(self, issue_uuid: str, text: str) -> bool:
        data = self._run("IssueComments", ISSUE_COMMENTS_GQL, {"id": issue_uuid})
        return any(text in (node.get("body") or "") for node in data["issue"]["comments"]["nodes"])

    def comment(self, issue_uuid: str, body: str) -> None:
        self._mutate(
            "CreateComment", CREATE_COMMENT_GQL, {"input": {"issueId": issue_uuid, "body": body}}, "commentCreate",
        )


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
