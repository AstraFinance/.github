import json

import pytest

from linear_release.linear_api import LinearClient, LinearError
from linear_release.model import Child, ReleaseRef

API_KEY = "lin_api_secret_value"


class FakePost:
    """Records each GraphQL request and answers from a queue of canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, body, headers):
        self.requests.append({"body": json.loads(body), "headers": headers})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _issue_node(**overrides):
    node = {
        "id": "uuid-2278",
        "identifier": "ENG-2278",
        "title": "Show closures",
        "url": "https://linear.app/astra/issue/ENG-2278",
        "state": {"name": "Ready for Deployment", "type": "started"},
        "parent": {"identifier": "ENG-1406"},
        "labels": {"nodes": [{"name": "Bug"}, {"name": "gated"}]},
        "children": {"nodes": [{"identifier": "ENG-9", "state": {"name": "Todo", "type": "unstarted"}}]},
        "releases": {
            "nodes": [
                {"id": "r1", "name": "Astra 2026.11.12", "url": "u1", "stage": {"type": "completed"}},
                {"id": "r2", "name": "Astra 2026.11.13", "url": "u2", "stage": {"type": "started"}},
            ]
        },
    }
    node.update(overrides)
    return node


def test_fetch_tickets_maps_labels_parent_children_and_completed_releases():
    """Given Linear returns an issue with mixed-case labels, a parent, a child and two releases,
    When tickets are fetched,
    Then labels are lower-cased, the parent and child are mapped, and only the completed release is kept."""
    post = FakePost({"data": {"issues": {"nodes": [_issue_node()]}}})
    tickets = LinearClient(API_KEY, post=post).fetch_tickets("ENG", ["ENG-2278"])

    t = tickets["ENG-2278"]
    assert t.uuid == "uuid-2278"
    assert t.labels == frozenset({"bug", "gated"})
    assert t.parent == "ENG-1406"
    assert t.state == "Ready for Deployment" and t.state_type == "started"
    assert t.children == (Child("ENG-9", "Todo", "unstarted"),)
    assert t.completed_releases == (ReleaseRef("r1", "Astra 2026.11.12", "u1"),)
    assert t.release_ids == frozenset({"r1", "r2"})


def test_fetch_tickets_includes_archived_releases():
    """Given releases can be archived after they complete,
    When tickets are fetched,
    Then archived releases still count toward a ticket's releases."""
    post = FakePost({"data": {"issues": {"nodes": []}}})
    LinearClient(API_KEY, post=post).fetch_tickets("ENG", ["ENG-1"])
    assert "releases(first: 50, includeArchived: true)" in post.requests[0]["body"]["query"]


def test_fetch_tickets_chunks_by_100():
    """Given 150 ticket ids,
    When tickets are fetched,
    Then two requests go out, each filtering by team key and numeric ticket numbers."""
    post = FakePost({"data": {"issues": {"nodes": []}}})
    LinearClient(API_KEY, post=post).fetch_tickets("ENG", [f"ENG-{n}" for n in range(1, 151)])

    assert len(post.requests) == 2
    first = post.requests[0]["body"]["variables"]
    assert first["teamKey"] == "ENG"
    assert first["numbers"][:2] == [1.0, 2.0] and len(first["numbers"]) == 100
    assert len(post.requests[1]["body"]["variables"]["numbers"]) == 50


def test_graphql_errors_raise_linear_error_without_the_key():
    """Given Linear answers with a GraphQL error,
    When any operation runs,
    Then a LinearError carries Linear's message and never the API key."""
    post = FakePost({"errors": [{"message": "boom"}]})
    with pytest.raises(LinearError) as excinfo:
        LinearClient(API_KEY, post=post).state_ids("ENG")
    assert "boom" in str(excinfo.value)
    assert API_KEY not in str(excinfo.value)


def test_requests_send_the_key_as_authorization():
    """Given a client built with an API key,
    When a request goes out,
    Then the key travels only in the Authorization header."""
    post = FakePost({"data": {"workflowStates": {"nodes": [{"id": "s1", "name": "Done"}]}}})
    assert LinearClient(API_KEY, post=post).state_ids("ENG") == {"Done": "s1"}
    assert post.requests[0]["headers"]["Authorization"] == API_KEY
    assert API_KEY not in json.dumps(post.requests[0]["body"])


def test_find_own_release_matches_the_marker():
    """Given the pipeline's release for this version was made by this workflow for this deploy,
    When this run's own release is looked up,
    Then it is returned with whether it is already completed."""
    post = FakePost({"data": {"releases": {"nodes": [
        {"id": "b", "name": "Astra 2026.11.04.2", "url": "ub",
         "description": "Helios v1.18.0\n\ncompare", "stage": {"type": "completed"}},
    ]}}})
    found = LinearClient(API_KEY, post=post).find_own_release("p1", "v1.18.0", "Helios v1.18.0")
    assert found == (ReleaseRef("b", "Astra 2026.11.04.2", "ub"), True)
    assert "includeArchived: true" in post.requests[0]["body"]["query"]


def test_find_own_release_stops_when_another_release_holds_the_version():
    """Given a release this workflow did not make already uses the deploy's version, which Linear keeps unique per pipeline,
    When this run's own release is looked up,
    Then the run stops before changing anything, naming the release and the version."""
    post = FakePost({"data": {"releases": {"nodes": [
        {"id": "a", "name": "Astra 2026.11.13", "url": "ua", "description": None, "stage": {"type": "started"}},
    ]}}})
    with pytest.raises(LinearError) as excinfo:
        LinearClient(API_KEY, post=post).find_own_release("p1", "1.192.0", "Delta 1.192.0")
    assert "Astra 2026.11.13" in str(excinfo.value) and "1.192.0" in str(excinfo.value)


def test_find_own_release_returns_none_without_match():
    """Given no release carries the marker,
    When this run's own release is looked up,
    Then nothing is returned."""
    post = FakePost({"data": {"releases": {"nodes": []}}})
    assert LinearClient(API_KEY, post=post).find_own_release("p1", "1.0.0", "Delta 1.0.0") is None


def test_attach_uses_added_release_ids():
    """Given a ticket and a release,
    When the ticket is attached,
    Then the update adds the release without replacing the ticket's other releases."""
    post = FakePost({"data": {"issueUpdate": {"success": True}}})
    LinearClient(API_KEY, post=post).attach("uuid-1", "r1")
    variables = post.requests[0]["body"]["variables"]
    assert variables == {"id": "uuid-1", "input": {"addedReleaseIds": ["r1"]}}


def test_create_release_starts_it_in_the_started_stage():
    """Given a pipeline and the release's fields,
    When the release is created,
    Then it opens in the started stage and the created release is returned."""
    from linear_release.linear_api import Pipeline

    post = FakePost({"data": {"releaseCreate": {
        "success": True, "release": {"id": "r9", "name": "Astra 2026.11.13", "url": "u9"},
    }}})
    pipeline = Pipeline("p1", "Astra", "s-start", "s-done")
    ref = LinearClient(API_KEY, post=post).create_release(
        pipeline, "Astra 2026.11.13", "1.192.0", "abc123", "Delta 1.192.0", "2026-11-13",
    )
    assert ref == ReleaseRef("r9", "Astra 2026.11.13", "u9")
    sent = post.requests[0]["body"]["variables"]["input"]
    assert sent["stageId"] == "s-start" and sent["pipelineId"] == "p1" and sent["version"] == "1.192.0"


def test_fetch_pipeline_picks_started_and_completed_stages():
    """Given a pipeline with planned, started, completed and canceled stages,
    When it is fetched by name,
    Then the started and completed stage ids are kept."""
    post = FakePost({"data": {"releasePipelines": {"nodes": [{
        "id": "p1", "name": "Astra",
        "stages": {"nodes": [
            {"id": "s-plan", "type": "planned"}, {"id": "s-start", "type": "started"},
            {"id": "s-done", "type": "completed"}, {"id": "s-cancel", "type": "canceled"},
        ]},
    }]}}})
    pipeline = LinearClient(API_KEY, post=post).fetch_pipeline("Astra")
    assert (pipeline.id, pipeline.started_stage_id, pipeline.completed_stage_id) == ("p1", "s-start", "s-done")


def test_fetch_pipeline_missing_raises():
    """Given no pipeline has the configured name,
    When it is fetched,
    Then the run stops with a LinearError naming the pipeline."""
    post = FakePost({"data": {"releasePipelines": {"nodes": []}}})
    with pytest.raises(LinearError, match="Internal Tooling"):
        LinearClient(API_KEY, post=post).fetch_pipeline("Internal Tooling")


def test_notes_status_missing_pending_completed():
    """Given a release with no note, then a pending note, then a finished one,
    When the notes status is read each time,
    Then it reports missing, pending, and completed with the note's text."""
    post = FakePost(
        {"data": {"release": {"releaseNotes": []}}},
        {"data": {"release": {"releaseNotes": [{"generationStatus": "pending", "documentContent": None}]}}},
        {"data": {"release": {"releaseNotes": [
            {"generationStatus": "completed", "documentContent": {"content": "## What changed"}},
        ]}}},
    )
    client = LinearClient(API_KEY, post=post)
    assert client.notes_status("r1") == ("missing", None)
    assert client.notes_status("r1") == ("pending", None)
    assert client.notes_status("r1") == ("completed", "## What changed")
