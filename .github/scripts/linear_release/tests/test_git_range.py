import pytest

from linear_release.git_range import previous_tag, ticket_ids


def test_previous_tag_plain_semver():
    """Given plain semver tags around the deployed tag,
    When the previous tag is resolved,
    Then it is the highest tag lower than the deployed one."""
    assert previous_tag(["1.189.0", "1.190.0", "1.191.0", "1.192.0"], "1.191.0") == "1.190.0"


def test_previous_tag_v_prefixed_ignores_other_style():
    """Given v-prefixed tags mixed with an unprefixed stray tag,
    When the previous tag of a v-prefixed tag is resolved,
    Then only v-prefixed tags are considered."""
    assert previous_tag(["1.0.0", "v1.17.0", "v1.18.0", "v1.9.9"], "v1.18.0") == "v1.17.0"


def test_previous_tag_compares_numerically():
    """Given versions whose text order differs from numeric order,
    When the previous tag is resolved,
    Then versions compare as numbers, not strings."""
    assert previous_tag(["1.9.0", "1.10.0"], "1.10.0") == "1.9.0"


def test_previous_tag_after_hotfix():
    """Given a hotfix tag cut after the last regular deploy,
    When the next regular deploy is resolved,
    Then its range starts at the hotfix tag."""
    assert previous_tag(["1.191.0", "1.191.1"], "1.192.0") == "1.191.1"


def test_previous_tag_without_lower_tag_raises():
    """Given no tag lower than the deployed tag,
    When the previous tag is resolved,
    Then it refuses rather than guessing a range."""
    with pytest.raises(ValueError):
        previous_tag(["1.191.0"], "1.191.0")


def test_ticket_ids_from_squash_and_merge_subjects():
    """Given squash-merge and merge-commit subjects, duplicates and another team's key,
    When ticket ids are extracted,
    Then each team ticket appears once, upper-cased, in first-seen order."""
    subjects = [
        "ENG-2278: Show client closures (#8519)",
        "Merge pull request #8510 from AstraFinance/feature/eng-2153-atomic-roles",
        "ENG-1966: Store the card network (#8512)",
        "ENG-1966: Define the contract (#8460)",
        "Bump deps",
        "OPS-12: other team",
    ]
    assert ticket_ids(subjects, "ENG") == ["ENG-2278", "ENG-2153", "ENG-1966"]
