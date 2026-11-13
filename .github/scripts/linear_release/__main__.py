"""Entry point: turn a deployed tag into Linear releases, status moves and a summary.

Usage: python -m linear_release --tag 1.192.0 --service Delta --repo AstraFinance/delta-backend [--dry-run]
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .apply import Context, apply_plan, find_own_releases
from .config import Config
from .git_range import previous_tag, read_commits, read_tags, tag_sha, ticket_ids
from .linear_api import LinearClient, LinearError
from .rules import build_plan
from .summary import render


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="linear_release")
    parser.add_argument("--repo-dir", default=".", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--previous-tag", default="")
    parser.add_argument("--service", required=True, choices=["Delta", "Helios"])
    parser.add_argument("--repo", required=True, help="owner/name, used for the compare link")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] = os.environ,
    client=None,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parse(argv)
    cfg = Config()

    api_key = env.get("LINEAR_API_KEY", "")
    if not api_key:
        print("error: LINEAR_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        tags = read_tags(args.repo_dir)
        if args.tag not in tags:
            print(f"error: tag {args.tag} does not exist in {args.repo_dir}", file=sys.stderr)
            return 2
        base = args.previous_tag or previous_tag(tags, args.tag)
        commits = read_commits(args.repo_dir, base, args.tag)
        ids = ticket_ids((c.subject for c in commits), cfg.team_key)
        unticketed = [c for c in commits if not ticket_ids([c.subject], cfg.team_key)]

        client = client or LinearClient(api_key)
        found = client.fetch_tickets(cfg.team_key, ids) if ids else {}
        not_found = [i for i in ids if i not in found]
        shipped = [found[i] for i in ids if i in found]

        parent_ids = sorted({t.parent for t in shipped if t.parent} - set(found))
        parents = client.fetch_tickets(cfg.team_key, parent_ids) if parent_ids else {}

        ctx = Context(
            service=args.service,
            tag=args.tag,
            previous_tag=base,
            commit_sha=tag_sha(args.repo_dir, args.tag),
            compare_url=f"https://github.com/{args.repo}/compare/{base}...{args.tag}",
            today=today or datetime.now(ZoneInfo(cfg.timezone)).date(),
            dry_run=args.dry_run,
        )
        own = find_own_releases(client, cfg, ctx) if shipped else {}
        plan = build_plan(shipped, parents, cfg, frozenset(ref.id for ref, _ in own.values()))
        outcome = apply_plan(plan, client, cfg, ctx, own, sleep=sleep)
    except (LinearError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    text = render(ctx, plan, outcome, not_found, unticketed)
    print(text)
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
