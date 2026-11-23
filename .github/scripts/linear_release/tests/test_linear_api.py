import json
import re

import pytest

from linear_release import linear_api
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


_LAST = {"hasNextPage": False, "endCursor": None}


def _issue_node(**overrides):
    node = {
        "id": "uuid-2278",
        "identifier": "ENG-2278",
        "title": "Show closures",
        "url": "https://linear.app/astra/issue/ENG-2278",
        "state": {"name": "Ready for Deployment", "type": "started"},
        "parent": {"identifier": "ENG-1406"},
        "labels": {"nodes": [{"name": "Bug"}, {"name": "gated"}], "pageInfo": _LAST},
        "children": {
            "nodes": [{"identifier": "ENG-9", "state": {"name": "Todo", "type": "unstarted"}}],
            "pageInfo": _LAST,
        },
        "releases": {
            "nodes": [
                {"id": "r1", "name": "Astra 2026.11.12", "url": "u1", "stage": {"type": "completed"}},
                {"id": "r2", "name": "Astra 2026.11.13", "url": "u2", "stage": {"type": "started"}},
            ],
            "pageInfo": _LAST,
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
    assert "releases(first: 25, includeArchived: true)" in post.requests[0]["body"]["query"]


def test_fetch_tickets_chunks_by_25():
    """Given 60 ticket ids,
    When tickets are fetched,
    Then three requests go out, each filtering by team key and at most 25 numeric ticket numbers."""
    post = FakePost({"data": {"issues": {"nodes": []}}})
    LinearClient(API_KEY, post=post).fetch_tickets("ENG", [f"ENG-{n}" for n in range(1, 61)])

    assert len(post.requests) == 3
    first = post.requests[0]["body"]["variables"]
    assert first["teamKey"] == "ENG"
    assert first["numbers"][:2] == [1.0, 2.0] and len(first["numbers"]) == 25
    assert len(post.requests[2]["body"]["variables"]["numbers"]) == 10


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


def test_release_issues_maps_identifiers_to_uuids():
    """Given a release with two tickets attached,
    When its tickets are read,
    Then each identifier maps to the ticket's id for later updates."""
    post = FakePost({"data": {"release": {"issues": {"nodes": [
        {"id": "uuid-1", "identifier": "ENG-1"}, {"id": "uuid-2", "identifier": "ENG-2"},
    ], "pageInfo": _LAST}}}})
    assert LinearClient(API_KEY, post=post).release_issues("r1") == {"ENG-1": "uuid-1", "ENG-2": "uuid-2"}


def test_detach_uses_removed_release_ids():
    """Given a ticket attached to a release,
    When it is detached,
    Then only that release is removed from the ticket."""
    post = FakePost({"data": {"issueUpdate": {"success": True}}})
    LinearClient(API_KEY, post=post).detach("uuid-1", "r1")
    assert post.requests[0]["body"]["variables"] == {"id": "uuid-1", "input": {"removedReleaseIds": ["r1"]}}


def test_has_comment_finds_an_earlier_comment_with_the_text():
    """Given a ticket whose comments include one from an earlier run,
    When the run checks for its comment,
    Then it finds it, and does not for text no comment contains."""
    post = FakePost({"data": {"issue": {"comments": {"nodes": [
        {"body": "Looks good"}, {"body": "**Linear release check · Delta 1.192.0**\n\nShipped..."},
    ]}}}})
    client = LinearClient(API_KEY, post=post)
    assert client.has_comment("uuid-1", "Linear release check · Delta 1.192.0") is True
    assert client.has_comment("uuid-1", "Linear release check · Delta 1.193.0") is False


def test_comment_posts_markdown_on_the_ticket():
    """Given a ticket and a message,
    When the run comments,
    Then a comment is created on that ticket with the message as its body."""
    post = FakePost({"data": {"commentCreate": {"success": True}}})
    LinearClient(API_KEY, post=post).comment("uuid-1", "Add a Release Method")
    assert post.requests[0]["body"]["variables"] == {"input": {"issueId": "uuid-1", "body": "Add a Release Method"}}


_TOKENS = re.compile(r"\{|\}|\([^()]*\)|[A-Za-z_]\w*")


def _selection_cost(tokens, i):
    """Score the fields up to the closing brace by Linear's rules; return the score, the next index, and
    whether the selection is a connection's (it lists `nodes`)."""
    cost, is_connection = 0.0, False
    while tokens[i] != "}":
        name, args = tokens[i], ""
        i += 1
        if tokens[i].startswith("("):
            args, i = tokens[i], i + 1
        if tokens[i] != "{":
            cost += 0.1
            continue
        child, i, child_is_connection = _selection_cost(tokens, i + 1)
        is_connection = is_connection or name == "nodes"
        if child_is_connection:
            first = re.search(r"first:\s*(\d+)", args)
            cost += (int(first.group(1)) if first else 50) * child
        else:
            cost += 1 + child
    return cost, i + 1, is_connection


def linear_complexity(document):
    """A query's complexity as Linear documents it: 0.1 per property, 1 per object, and a connection's
    children multiplied by its `first:`, or 50. pageInfo is scored once per node, which overstates it."""
    tokens = _TOKENS.findall(document)
    body = tokens.index("{", tokens.index("query") if "query" in tokens else 0)
    return _selection_cost(tokens, body + 1)[0]


def _queries():
    documents = {name: value for name, value in vars(linear_api).items() if name.endswith("_GQL") and isinstance(value, str)}
    documents.update({f"ISSUE_PAGE_GQL[{field}]": value for field, value in linear_api.ISSUE_PAGE_GQL.items()})
    return {name: doc for name, doc in documents.items() if doc.lstrip().startswith("query")}


def test_the_complexity_estimate_matches_linears_documented_examples():
    """Given the queries Linear's rate-limiting page scores,
    When their complexity is estimated,
    Then the estimate matches the page: 1.1 for a user's name and 14 for ten of their issues' three fields."""
    assert linear_complexity('query { user(id: "me") { name } }') == pytest.approx(1.1)
    assert linear_complexity(
        'query { user(id: "me") { createdIssues(first: 10) { nodes { id title createdAt } } } }'
    ) == pytest.approx(14)


@pytest.mark.parametrize("name", sorted(_queries()))
def test_every_query_stays_under_linears_complexity_limit(name):
    """Given Linear rejects any query above 10,000 complexity points, scored from its declared bounds,
    When each query the client sends is scored,
    Then it stays under the limit, whatever a run asks for."""
    assert linear_complexity(_queries()[name]) < 10_000


def _page(field, nodes, cursor=None):
    return {"data": {"issue": {field: {
        "nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
    }}}}


def test_fetch_tickets_reads_every_child_before_a_parent_is_planned():
    """Given a parent whose first page of children is all done and whose next page holds an open child,
    When tickets are fetched,
    Then the next page is read from the first page's cursor and the open child is among the parent's children."""
    done = {"identifier": "ENG-10", "state": {"name": "Done", "type": "completed"}}
    open_child = {"identifier": "ENG-11", "state": {"name": "Todo", "type": "unstarted"}}
    node = _issue_node(children={"nodes": [done], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}})
    post = FakePost({"data": {"issues": {"nodes": [node]}}}, _page("children", [open_child]))

    t = LinearClient(API_KEY, post=post).fetch_tickets("ENG", ["ENG-2278"])["ENG-2278"]

    assert post.requests[1]["body"]["variables"] == {"id": "uuid-2278", "after": "c1"}
    assert t.children == (Child("ENG-10", "Done", "completed"), Child("ENG-11", "Todo", "unstarted"))


def test_fetch_tickets_reads_a_label_past_the_first_page():
    """Given a ticket whose gated label is on its second page of labels,
    When tickets are fetched,
    Then the ticket's labels include gated."""
    node = _issue_node(labels={"nodes": [{"name": "Bug"}], "pageInfo": {"hasNextPage": True, "endCursor": "l1"}})
    post = FakePost({"data": {"issues": {"nodes": [node]}}}, _page("labels", [{"name": "Gated"}]))

    t = LinearClient(API_KEY, post=post).fetch_tickets("ENG", ["ENG-2278"])["ENG-2278"]

    assert t.labels == frozenset({"bug", "gated"})


def test_release_issues_reads_every_page():
    """Given a release whose attached tickets span two pages,
    When its tickets are read,
    Then the second page is read from the first page's cursor and both pages' tickets are returned."""
    post = FakePost(
        {"data": {"release": {"issues": {
            "nodes": [{"id": "uuid-1", "identifier": "ENG-1"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "p1"},
        }}}},
        {"data": {"release": {"issues": {"nodes": [{"id": "uuid-2", "identifier": "ENG-2"}], "pageInfo": _LAST}}}},
    )

    attached = LinearClient(API_KEY, post=post).release_issues("r1")

    assert post.requests[1]["body"]["variables"] == {"id": "r1", "after": "p1"}
    assert attached == {"ENG-1": "uuid-1", "ENG-2": "uuid-2"}


@pytest.mark.parametrize(("stage", "completed"), [("completed", True), ("started", False)])
def test_release_completed_reads_the_stage_now(stage, completed):
    """Given a release in a stage,
    When whether it is completed is read,
    Then only the completed stage counts."""
    post = FakePost({"data": {"release": {"stage": {"type": stage}}}})
    assert LinearClient(API_KEY, post=post).release_completed("r1") is completed
