"""Secrets-hygiene checks over the actual repo files, not mocks -- the one
security-relevant surface in this project (product.md/decisions.md): the
repo is public, the key must never leak, and no workflow may let attacker-
controlled issue text run inside a shell.

These are static checks against committed files. They complement, not
replace, the live verification in Phase 6's plan (`gh secret list`, reading a
full run log for the literal key, `curl -I` for the noindex headers).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# A real Gemini API key looks like AIza + 35 more chars. Matching this shape
# in committed output would mean a key leaked into the repo.
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")


def test_no_workflow_uses_pull_request_target():
    for path in WORKFLOWS:
        assert "pull_request_target" not in path.read_text(), f"{path.name} uses pull_request_target"


def test_no_run_block_interpolates_raw_issue_text():
    """github.event.issue.title/body is attacker-controlled text on a public
    repo's issue tracker; interpolating it into a `run:` shell block is a
    script-injection risk. follow.yml reads the issue body via the GitHub API
    in Python instead (follow.fetch_issues) -- it must never appear inline in
    a run: step. The `if:` job condition matching on labels.*.name is a
    separate, safe context (evaluated by the Actions expression engine, never
    shelled out) and is not what this guards against."""
    for path in WORKFLOWS:
        text = path.read_text()
        assert "github.event.issue.title" not in text, f"{path.name} references issue title"
        assert "github.event.issue.body" not in text, f"{path.name} references issue body"


def test_secret_only_ever_assigned_via_env():
    """Every mention of the Gemini key secret must be on an `env:` mapping
    line (`GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}`), never inside a
    run: shell string where it could be echoed or logged."""
    for path in WORKFLOWS:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "secrets.GEMINI_API_KEY" in line:
                stripped = line.strip()
                assert stripped.startswith("GEMINI_API_KEY:"), (
                    f"{path.name}:{lineno} references the key outside an env: assignment: {line!r}"
                )


def test_follow_workflow_guards_on_repo_owner():
    follow_yml = (ROOT / ".github" / "workflows" / "follow.yml").read_text()
    assert "github.event.issue.user.login == 'SameerJadav'" in follow_yml


def test_no_leaked_key_shape_in_committed_output():
    # debug/ is included because it is committed like the rest, and unlike
    # the rest it carries raw model prompts and responses -- the one place a
    # key could plausibly turn up. tracer.scrub() is what keeps it clean;
    # this is the check that the scrubbing actually held.
    for sub in ("data", "docs", "followed", "debug"):
        d = ROOT / sub
        if not d.exists():
            continue
        for path in d.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".html", ".js", ".css", ".txt",
                                                  ".jsonl", ".xml", ".md", ".wikitext"}:
                assert not _KEY_RE.search(path.read_text(errors="ignore")), f"key-shaped string found in {path}"


def test_debug_capture_is_off_by_default():
    """The switch defaults to off in code, so only digest.yml's explicit
    DIGEST_DEBUG turns it on. Flipping that one line back to "0" must be
    enough to stop all capture."""
    import os

    import tracer

    saved = os.environ.pop("DIGEST_DEBUG", None)
    try:
        assert tracer._level_from_env() == 0
    finally:
        if saved is not None:
            os.environ["DIGEST_DEBUG"] = saved


def test_digest_workflow_commits_debug_dir():
    """Capture that never leaves the runner is worthless — the whole point
    is that it lands in the repo where it can be read afterwards."""
    digest_yml = (ROOT / ".github" / "workflows" / "digest.yml").read_text()
    assert "DIGEST_DEBUG:" in digest_yml
    assert "git add data docs followed debug" in digest_yml
    assert "mkdir -p data docs followed debug" in digest_yml


def test_workflow_permissions_stay_least_privilege():
    """Every workflow declares its scopes explicitly, so anything unlisted is
    `none`. `issues: write` is on digest.yml deliberately (the daily sweep is
    where a stale follow's auto-close fires, and it 403'd for eleven days
    without it — ANALYSIS-2026-08-23.md §M1); a blanket `write-all`, or a new
    scope nobody argued for, is what this catches."""
    allowed = {"contents: write", "issues: write", "pages: write", "id-token: write"}
    for path in WORKFLOWS:
        text = path.read_text()
        block = re.search(r"^permissions:\n((?:  \S.*\n)+)", text, re.MULTILINE)
        assert block, f"{path.name} has no top-level permissions block"
        scopes = {line.strip() for line in block.group(1).splitlines() if line.strip()}
        assert scopes <= allowed, f"{path.name} grants {scopes - allowed}"
