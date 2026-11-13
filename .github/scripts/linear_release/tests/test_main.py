import subprocess
from datetime import date

import pytest

from linear_release.__main__ import main

from .factories import ticket
from .fakes import FakeLinear

MUTATIONS = {"create_release", "attach", "set_state", "complete_release"}


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A repository whose v1.18.0 tag adds the given commit subjects on top of v1.17.0."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "Start")
    _git(path, "tag", "v1.17.0")

    def build(*subjects):
        for subject in subjects:
            _git(path, "commit", "-q", "--allow-empty", "-m", subject)
        _git(path, "tag", "v1.18.0")
        return path

    return build


def _run(repo_path, fake, tmp_path, *extra, env=None):
    summary = tmp_path / "summary.md"
    argv = ["--repo-dir", str(repo_path), "--tag", "v1.18.0", "--service", "Helios",
            "--repo", "AstraFinance/astra-helios", *extra]
    environment = {"LINEAR_API_KEY": "test-key", "GITHUB_STEP_SUMMARY": str(summary)} if env is None else env
    code = main(argv, env=environment, client=fake, today=date(2026, 11, 4), sleep=lambda s: None)
    return code, summary.read_text() if summary.exists() else ""


def test_main_dry_run_end_to_end(repo, tmp_path):
    """Given a tag range with two ticketed commits and a dry run,
    When the entry point runs,
    Then it exits cleanly, writes the summary and changes nothing in Linear."""
    path = repo("ENG-1: Fix the percentile buckets (#1)", "ENG-2: Open the staging route (#2)")
    fake = FakeLinear(tickets=[ticket("ENG-1", "immediate", "bug"), ticket("ENG-2", "immediate", "internal")])

    code, summary = _run(path, fake, tmp_path, "--dry-run")

    assert code == 0
    assert "Linear release · Helios v1.18.0 (dry run)" in summary
    assert "v1.17.0 → v1.18.0" in summary
    assert "Astra 2026.11.04" in summary and "Internal 2026.11.04" in summary
    assert not MUTATIONS & set(fake.names())


def test_main_applies_the_plan_when_not_a_dry_run(repo, tmp_path):
    """Given a tag range with one releasable bug and no dry run,
    When the entry point runs,
    Then the release is created with the tagged commit and the ticket is moved."""
    path = repo("ENG-1: Fix the percentile buckets (#1)")
    fake = FakeLinear(tickets=[ticket("ENG-1", "immediate", "bug")])

    code, summary = _run(path, fake, tmp_path)

    assert code == 0
    create = next(args for name, args in fake.calls if name == "create_release")
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "v1.18.0"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert create[3] == head
    assert ("set_state", ("uuid-ENG-1", "s-done")) in fake.calls
    assert "## What changed" in summary


def test_main_with_no_ticket_ids_succeeds_without_release(repo, tmp_path):
    """Given a tag range whose commits name no ticket,
    When the entry point runs,
    Then it succeeds without touching Linear and lists the commits."""
    path = repo("Bump dependencies", "Fix a typo in the README")
    fake = FakeLinear()

    code, summary = _run(path, fake, tmp_path)

    assert code == 0
    assert "create_release" not in fake.names()
    assert "Bump dependencies" in summary and "Fix a typo in the README" in summary


def test_unknown_ticket_is_reported_not_fatal(repo, tmp_path):
    """Given a commit naming a ticket Linear does not return,
    When the entry point runs,
    Then the run still succeeds and the ticket is listed as not found."""
    path = repo("ENG-99999: Mistyped ticket (#3)", "ENG-1: Fix the percentile buckets (#1)")
    fake = FakeLinear(tickets=[ticket("ENG-1", "immediate", "bug")])

    code, summary = _run(path, fake, tmp_path)

    assert code == 0
    assert "### Not found in Linear" in summary and "ENG-99999" in summary


def test_main_requires_api_key(repo, tmp_path, capsys):
    """Given no LINEAR_API_KEY in the environment,
    When the entry point runs,
    Then it stops with exit code 2 and names the missing variable."""
    path = repo("ENG-1: Fix (#1)")

    code, _ = _run(path, FakeLinear(), tmp_path, env={})

    assert code == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err


def test_main_rejects_an_unknown_tag(repo, tmp_path, capsys):
    """Given a tag that does not exist in the repository,
    When the entry point runs,
    Then it stops with exit code 2 before calling Linear."""
    path = repo("ENG-1: Fix (#1)")
    fake = FakeLinear()
    summary = tmp_path / "summary.md"

    code = main(["--repo-dir", str(path), "--tag", "v9.9.9", "--service", "Helios", "--repo", "o/r"],
                env={"LINEAR_API_KEY": "k", "GITHUB_STEP_SUMMARY": str(summary)}, client=fake)

    assert code == 2
    assert "v9.9.9" in capsys.readouterr().err
    assert fake.calls == []
