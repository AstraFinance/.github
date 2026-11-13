"""The rules replayed against two real deploys, with labels as they stood in Linear.

Shipped tickets are fixtured in Ready for Deployment, their state before the
deploy, and each parent carries one of its real open sub-issues.
"""

from linear_release.rules import build_plan

from .factories import CFG, child, ids, moves, ticket


def _states(plan, state):
    return {identifier for identifier, to in moves(plan).items() if to == state}


def test_replay_delta_1_191_0():
    """Given the thirteen tickets in delta-backend's 1.190.0..1.191.0 range,
    When the plan is built,
    Then no release is made, gated work stops at Deployed, and only the unlabeled bug is flagged."""
    shipped = [
        ticket("ENG-2278", "bug", "internal", "vector", "gated", "low-risk", "repo:delta-backend",
               "feature-flagged", parent="ENG-1406"),
        ticket("ENG-1225", "platform-health", "gated", parent="ENG-1031"),
        ticket("ENG-847", "area:parking", "repo:delta-backend", "immediate", "low-risk"),
        ticket("ENG-2131", "platform-health", "low-risk", "bug"),
        ticket("ENG-2143", "bug", "immediate", "high-risk", "risk:money-movement", "repo:delta-backend",
               "payments-core", parent="ENG-1406"),
        ticket("ENG-1966", "task", "immediate", "low-risk", "repo:delta-backend", parent="ENG-1233"),
        ticket("ENG-1948", "customer friction", "legal-compliance", "gated", "low-risk", parent="ENG-1406"),
        ticket("ENG-1437", "dual-sponsor", parent="ENG-1669"),
        ticket("ENG-1979", "immediate", "high-risk", parent="ENG-1406"),
        ticket("ENG-1949", "customer friction", "legal-compliance", "low-risk", "gated", parent="ENG-1406"),
        ticket("ENG-1027", "gated", "settlement", "payments-core"),
        ticket("ENG-1126", "low-risk", "immediate", parent="ENG-1646"),
        ticket("ENG-1947", "customer friction", "legal-compliance", "low-risk", "gated", parent="ENG-1406"),
    ]
    parents = {
        "ENG-1406": ticket("ENG-1406", "customer friction", "legal-compliance", state="In Review",
                           children=(child("ENG-2232", "Ready for Deployment"),)),
        "ENG-1031": ticket("ENG-1031", "gated", "payments-core", state="In Review",
                           children=(child("ENG-1671", "In Review"),)),
        "ENG-1233": ticket("ENG-1233", state="In Progress",
                           children=(child("ENG-1230", "Todo", "unstarted"),)),
        "ENG-1669": ticket("ENG-1669", "dual-sponsor", state="Todo", state_type="unstarted",
                           children=(child("ENG-2305", "Todo", "unstarted"),)),
        "ENG-1646": ticket("ENG-1646", "dual-sponsor", state="In Progress",
                           children=(child("ENG-2098", "Todo", "unstarted"),)),
    }

    plan = build_plan(shipped, parents, CFG)

    assert plan.releases == {}
    assert _states(plan, "Done") == {"ENG-847", "ENG-2143", "ENG-1966", "ENG-1979", "ENG-1126", "ENG-1437"}
    assert _states(plan, "Deployed") == {
        "ENG-2278", "ENG-1225", "ENG-1948", "ENG-1949", "ENG-1027", "ENG-1947", "ENG-2131",
    }
    assert [n.identifier for n in plan.notes if n.attention] == ["ENG-2131"]


def test_replay_helios_v1_18_0():
    """Given the five tickets in astra-helios's v1.17.0..v1.18.0 range,
    When the plan is built,
    Then the unlabeled metrics bug goes to Astra, the internal ticket to Internal, and the gated one stops at Deployed."""
    shipped = [
        ticket("ENG-2103", "bug", "platform-health", "immediate", "low-risk"),
        ticket("ENG-1686", "dual-sponsor", parent="ENG-1368"),
        ticket("ENG-2044", "internal", "immediate", "low-risk"),
        ticket("ENG-1023", "gated", "settlement", "payments-core", "high-risk"),
        ticket("ENG-2003", "dual-sponsor", parent="ENG-1368"),
    ]
    parents = {
        "ENG-1368": ticket("ENG-1368", "dual-sponsor", state="In Progress",
                           children=(child("ENG-2264", "Todo", "unstarted"),)),
    }

    plan = build_plan(shipped, parents, CFG)

    assert ids(plan.releases["astra"]) == ["ENG-2103"]
    assert ids(plan.releases["internal"]) == ["ENG-2044"]
    assert _states(plan, "Done") == {"ENG-2103", "ENG-2044", "ENG-1686", "ENG-2003"}
    assert _states(plan, "Deployed") == {"ENG-1023"}
