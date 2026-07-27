"""The Follow domain: turning a prefilled GitHub issue into a followed-story
page that grows a day at a time.

`followed/<issue>.json` is a source of truth alongside `data/`; every page in
docs/ that concerns a followed story is derived from it and is rebuilt
wholesale by render.py. Only this module writes followed/.

product.md is emphatic that **nothing follows itself** — a record here exists
only because the repo owner opened an issue labelled "follow", and every
GitHub-facing function in this module re-checks that before doing anything.
The workflow-level `github.event.issue.user.login == 'SameerJadav'` guard in
follow.yml is the first gate; fetch_issues()'s filter below is the second,
so the guard holds even if this is ever invoked outside that workflow.

Follows are uncapped (decisions.md), so quota is protected by batching the
daily timeline pass across every active follow in ONE grounded call, never
one call per story — mirroring llm.py's two/three-calls-a-day discipline.
The backstory is generated exactly once per follow and is never regenerated;
regenerating it would both burn quota and break the "grows the fuller
picture you already have" promise in product.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

import ground
import tracer
from tracer import dbg
from ground import GroundedBlock
from rank import SECTIONS

OWNER = "SameerJadav"
REPO = "SameerJadav/follow-news"
FOLLOW_LABEL = "follow"
BASE_URL = "https://sameerjadav.github.io/follow-news/"

STALE_DAYS = 14  # decisions.md: auto-close after ~14 days with no development
MAX_FOLLOWS_PER_BATCH = 6  # follows are uncapped; grounded calls per run are not
MAX_NEW_FOLLOWS_PER_RUN = 3  # a burst of new follows cannot drain a morning's quota
TIMELINE_RECAP_ENTRIES = 6  # how much prior timeline the batch prompt carries per story

_API = "https://api.github.com"
_REQUEST_TIMEOUT = 15

_FIELD_RE = re.compile(r"^\s*(digest|section|story|headline)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS_RE = re.compile(r"\s+")

_PROSE_RULES = """\
Write in plain adult English for a non-native reader: short sentences, \
active voice, concrete nouns, no jargon, no idioms. Clear, but not \
simplified — a good explainer site, not a children's news service.

PLAIN TEXT ONLY. Never use markdown — no headings, no "#", no "*" or "-" \
bullets, no bold, no links. Your output is inserted directly into a web \
page as prose; any markup would appear as literal characters.

Write one continuous piece of prose per story, paragraphs separated by a \
single blank line. Never use a heading, a label, or a section such as \
"Why this matters" or "What to watch".

Never present a contested claim as settled. Where sources disagree, or a \
fact is somebody's assertion, keep the attribution — "the ministry says", \
"the BBC reported" — and say plainly where reporting disagrees or facts \
are still uncertain.

Never blend or average a figure from two sources into one number. If \
sources disagree on a number, state both and attribute each."""

_BACKSTORY_SYSTEM = f"""You are writing the full-picture explainer for a \
story someone has chosen to follow closely. Research it using Google \
Search, from wherever it ACTUALLY BEGAN — even if that is months or years \
before the given date. The reader must never be dropped into the middle of \
something with missing backstory.

{_PROSE_RULES}

Write chronologically: how it started, what drove it, what has happened \
since, and where it stands as of the given date. Assume the reader has \
read nothing about this before — not even the news story that made them \
want to follow it.

Length: 500-700 words. Close by stating plainly where things stand as of \
the given date."""

_TIMELINE_SYSTEM = f"""You maintain daily timelines for several followed \
news stories at once, using Google Search. For EACH story block in the \
input, in the exact same order, emit a block starting with the identical \
"=== FOLLOW <key> ===" header line as its input block, then a line reading \
exactly one of:

STATUS: development
STATUS: quiet
STATUS: final

Use STATUS: quiet when nothing significant happened in the covered period \
for that story — then write nothing more for that block. A quiet period is \
a correct, expected answer; never invent an update to fill it.

Use STATUS: development when something new happened. Write 80-150 words \
covering ONLY what is new since the period given for that story — never \
restate the backstory or an earlier update. List EVERY significant \
development in the period, not only the single biggest one: leaving one \
out is as serious an error as getting one wrong.

Use STATUS: final ONLY for a block whose input is marked "MODE: final". \
Write an 80-150 word close: how the story ended and where things stand \
now. Introduce no new claims beyond wrapping up what is already known.

{_PROSE_RULES}"""


# ---------- the followed/ contract ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_label(d: date) -> str:
    return d.strftime("%A, %-d %B %Y")


def _block_to_dict(block: GroundedBlock, generated_at: datetime) -> dict:
    d = asdict(block)
    d["generated_at"] = _iso(generated_at)
    return d


def load_all(followed_dir: Path) -> dict[int, dict]:
    """Every followed/*.json, keyed by issue number. Returns {} if the
    directory doesn't exist yet — Follow has never run."""
    if not followed_dir.exists():
        return {}
    records: dict[int, dict] = {}
    for path in sorted(followed_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
            records[int(record["issue"])] = record
        except (OSError, ValueError, KeyError) as exc:
            dbg(f"follow: could not load {path} ({exc!r}); skipping")
    return records


def _write_record(followed_dir: Path, record: dict) -> None:
    followed_dir.mkdir(parents=True, exist_ok=True)
    path = followed_dir / f"{record['issue']}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    dbg(f"follow: wrote {path}")


# ---------- the prefilled issue URL (mirrored by render.py) ----------


def issue_url(day_date: str, section: str, position: int, headline: str) -> str:
    """The prefilled GitHub issue URL a Follow button opens. render.py builds
    this same URL at render time from data on the <article> element — there
    is no slug or id computed anywhere else, so this is the one place the
    shape of a follow request is defined."""
    title = f"Follow: {headline}"[:120]
    body = (
        "Follow this story.\n\n"
        f"digest: {day_date}\n"
        f"section: {section}\n"
        f"story: {position}\n"
        f"headline: {headline}"
    )
    params = f"labels={quote(FOLLOW_LABEL, safe='')}&title={quote(title, safe='')}&body={quote(body, safe='')}"
    return f"https://github.com/{REPO}/issues/new?{params}"


# ---------- parsing an issue body back into a story reference ----------


def parse_request(body: str) -> dict | None:
    """Pull digest/section/story/headline fields out of an issue body. The
    body is attacker-controlled text from a public repo's issue tracker, so
    this only ever extracts a few narrow fields by regex — nothing here is
    executed or interpolated into a shell or URL."""
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(body or ""):
        key = m.group(1).lower()
        if key not in fields:
            fields[key] = m.group(2).strip()

    digest = fields.get("digest", "")
    if not _DATE_RE.match(digest):
        return None

    section = fields.get("section", "").lower()
    story_raw = fields.get("story", "")
    headline = fields.get("headline", "")

    has_position = section in SECTIONS and story_raw.isdigit() and int(story_raw) >= 1
    if not has_position and not headline:
        return None

    return {
        "digest": digest,
        "section": section if section in SECTIONS else "",
        "story": int(story_raw) if story_raw.isdigit() else None,
        "headline": headline,
    }


def _normalise(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().casefold()


def resolve(request: dict, data_dir: Path) -> dict | None:
    """Match a parsed follow request against data/<digest>.json. Tries the
    stated section+position first (falling back to a headline check when one
    was supplied, so a story that has since shifted position still
    resolves), then falls back to scanning every story in the file for a
    matching headline."""
    path = data_dir / f"{request['digest']}.json"
    if not path.exists():
        dbg(f"follow: resolve -> {path} does not exist")
        return None

    try:
        day = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        dbg(f"follow: resolve -> could not read {path} ({exc!r})")
        return None

    stories = day.get("stories") or []
    wanted_headline = _normalise(request["headline"]) if request["headline"] else None

    if request["section"] and request["story"]:
        section_stories = [s for s in stories if s.get("section") == request["section"]]
        idx = request["story"] - 1
        if 0 <= idx < len(section_stories):
            candidate = section_stories[idx]
            if wanted_headline is None or _normalise(str(candidate.get("headline", ""))) == wanted_headline:
                dbg(f"follow: resolve -> matched by position ({request['section']} #{request['story']})")
                return {
                    "date": request["digest"],
                    "section": candidate.get("section", request["section"]),
                    "position": request["story"],
                    "headline": str(candidate.get("headline", "")),
                }

    if wanted_headline:
        for section_key in SECTIONS:
            section_stories = [s for s in stories if s.get("section") == section_key]
            for i, s in enumerate(section_stories, start=1):
                if _normalise(str(s.get("headline", ""))) == wanted_headline:
                    dbg(f"follow: resolve -> matched by headline scan ({section_key} #{i})")
                    return {
                        "date": request["digest"],
                        "section": section_key,
                        "position": i,
                        "headline": str(s.get("headline", "")),
                    }

    dbg(f"follow: resolve -> no story matched for digest={request['digest']!r} section={request['section']!r} story={request['story']!r} headline={request['headline']!r}")
    return None


# ---------- GitHub API access ----------


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_issues() -> list[dict]:
    """Every issue labelled "follow", open or closed. Filters to the repo
    owner and drops pull requests (the issues endpoint returns both) — this
    is the security boundary in Python, mirroring follow.yml's job-level
    `if:` guard. Returns [] on any failure; Follow must never take the
    digest down."""
    try:
        resp = requests.get(
            f"{_API}/repos/{REPO}/issues",
            params={"labels": FOLLOW_LABEL, "state": "all", "per_page": 100},
            headers=_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        issues = resp.json()
    except (requests.RequestException, ValueError) as exc:
        dbg(f"follow: fetch_issues -> failed ({exc!r}); treating as no issues")
        return []

    owned: list[dict] = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        login = (issue.get("user") or {}).get("login")
        if login != OWNER:
            dbg(f"follow: ignoring issue #{issue.get('number')} from {login!r} — not the repo owner")
            continue
        owned.append(issue)
    return owned


def comment(issue: int, text: str) -> None:
    """Best-effort: a failed comment must never fail the run. The published
    page is the product; the comment is a courtesy."""
    try:
        resp = requests.post(
            f"{_API}/repos/{REPO}/issues/{issue}/comments",
            json={"body": text},
            headers=_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        dbg(f"follow: could not comment on #{issue} ({exc!r})")


def close_issue(issue: int, text: str | None = None) -> None:
    if text:
        comment(issue, text)
    try:
        resp = requests.patch(
            f"{_API}/repos/{REPO}/issues/{issue}",
            json={"state": "closed"},
            headers=_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        dbg(f"follow: could not close #{issue} ({exc!r})")


# ---------- orchestration ----------


def _new_follows(records: dict[int, dict], issues: list[dict], data_dir: Path) -> None:
    open_new = [i for i in issues if i.get("state") == "open" and int(i["number"]) not in records]
    if len(open_new) > MAX_NEW_FOLLOWS_PER_RUN:
        deferred = [i["number"] for i in open_new[MAX_NEW_FOLLOWS_PER_RUN:]]
        dbg(f"follow: {len(open_new)} new follow request(s); deferring {deferred} to a later run")
    for issue in open_new[:MAX_NEW_FOLLOWS_PER_RUN]:
        n = int(issue["number"])
        request = parse_request(issue.get("body") or "")
        if request is None:
            dbg(f"follow: #{n} -> body did not parse as a follow request")
            close_issue(n, "Could not tell which story this refers to — closing. Try following again from the digest page.")
            continue

        resolved = resolve(request, data_dir)
        if resolved is None:
            dbg(f"follow: #{n} -> could not resolve to a story")
            close_issue(n, f"Could not find that story in the {request['digest']} digest — closing. Try following again from the digest page.")
            continue

        prompt = (
            f"STORY: {resolved['headline']}\n"
            f"AS FIRST REPORTED: {resolved['date']} ({resolved['section']} section)\n\n"
            "Research and write the full-picture explainer for this story."
        )
        try:
            backstory = ground.research(prompt, _BACKSTORY_SYSTEM, f"backstory-{n}")
        except Exception as exc:  # noqa: BLE001
            dbg(f"follow: #{n} -> backstory generation failed ({exc!r}); leaving open for a later run")
            continue

        if backstory is None:
            dbg(f"follow: #{n} -> backstory came back empty/ungrounded; leaving open for a later run")
            continue

        now = _now()
        record = {
            "issue": n,
            "status": "active",
            "title": resolved["headline"],
            "section": resolved["section"],
            "origin": {
                "date": resolved["date"],
                "section": resolved["section"],
                "position": resolved["position"],
                "headline": resolved["headline"],
            },
            "started_at": _iso(now),
            "closed_at": None,
            "close_reason": None,
            "last_development": resolved["date"],
            "backstory": _block_to_dict(backstory, now),
            "timeline": [],
        }
        records[n] = record
        comment(n, f"Following: {BASE_URL}follow-{n}.html")
        dbg(f"follow: #{n} -> new follow started for {resolved['headline']!r}")


def _unfollow_closed(records: dict[int, dict], issues: list[dict], now: datetime) -> None:
    by_number = {int(i["number"]): i for i in issues}
    for n, record in records.items():
        if record.get("status") != "active":
            continue
        issue = by_number.get(n)
        if issue is not None and issue.get("state") == "closed":
            record["status"] = "closed"
            record["close_reason"] = "unfollowed"
            record["closed_at"] = _iso(now)
            dbg(f"follow: #{n} -> issue closed by owner, marking unfollowed")


def _needs_timeline_pass(last_development: str, today: date) -> bool:
    """A follow is due for a timeline pass only once at least a day has
    passed since its last recorded development. Without this, a follow
    created THIS run has last_development == today and an empty timeline —
    "no entry dated today yet" would otherwise look due — and a same-run
    timeline call for it could only ever come back quiet, since the
    backstory just covered "as of today". Verified live (2026-07-25,
    follow-news issue #1): the very first Follow run made exactly this
    wasted call before this guard was added."""
    return last_development < today.isoformat()


def _is_closing(last_development: str, today: date) -> bool:
    """decisions.md: auto-close after ~14 days with no significant
    development. A named, tunable predicate (mirrors rank.py's dials and
    anchor.py's thresholds) rather than inline arithmetic, so Phase 6 can
    calibrate STALE_DAYS without touching _timeline_pass, and so it can be
    tested without a network call."""
    last_dev = datetime.strptime(last_development, "%Y-%m-%d").date()
    return (today - last_dev).days >= STALE_DAYS


def _recap_lines(record: dict) -> str:
    entries = record.get("timeline") or []
    recent = entries[-TIMELINE_RECAP_ENTRIES:]
    if not recent:
        return "(none yet)"
    lines = []
    for e in recent:
        first_sentence = (e.get("body") or "").split(". ", 1)[0].strip()
        lines.append(f"- {e.get('date', '?')}: {first_sentence}")
    return "\n".join(lines)


def _timeline_pass(records: dict[int, dict], today: date, batch: list[int]) -> None:
    if not batch:
        return

    blocks = []
    closing: dict[int, bool] = {}
    for n in batch:
        record = records[n]
        is_closing = _is_closing(record["last_development"], today)
        closing[n] = is_closing
        blocks.append(
            f"=== FOLLOW {n} ===\n"
            f"MODE: {'final' if is_closing else 'update'}\n"
            f"TITLE: {record['title']}\n"
            f"STORY BEGAN: {record['origin']['date']}\n"
            f"COVERED THROUGH: {record['last_development']}\n"
            f"ALREADY COVERED:\n{_recap_lines(record)}\n"
            f"Report only what is new after {record['last_development']}."
        )
    prompt = "\n\n".join(blocks)

    try:
        results = ground.research_batch(prompt, _TIMELINE_SYSTEM, "timeline", [str(n) for n in batch])
    except Exception as exc:  # noqa: BLE001
        dbg(f"follow: timeline batch failed ({exc!r}); leaving these follows unchanged")
        return

    now = _now()
    for n in batch:
        status, block = results.get(str(n), ("quiet", None))
        record = records[n]

        if status == "development" and block is not None:
            entry = _block_to_dict(block, now)
            entry["date"] = today.isoformat()
            entry["date_label"] = _date_label(today)
            entry["kind"] = "development"
            record["timeline"].append(entry)
            record["last_development"] = today.isoformat()
            dbg(f"follow: #{n} -> timeline entry added ({today})")

        elif status == "final" and block is not None:
            if not closing[n]:
                dbg(f"follow: #{n} -> model returned STATUS: final but story wasn't due to close; treating as quiet")
                continue
            entry = _block_to_dict(block, now)
            entry["date"] = today.isoformat()
            entry["date_label"] = _date_label(today)
            entry["kind"] = "final"
            record["timeline"].append(entry)
            record["status"] = "closed"
            record["close_reason"] = "no_development"
            record["closed_at"] = _iso(now)
            close_issue(n, f"No new developments for {STALE_DAYS} days — closing. Final update: {BASE_URL}follow-{n}.html")
            dbg(f"follow: #{n} -> closed, no development for {STALE_DAYS}+ days")

        # status == "quiet" (or a status/block mismatch): nothing to do. A
        # quiet day appends no entry — silence, not a "nothing happened" line.


def run(data_dir: Path, followed_dir: Path, docs_dir: Path, today: date, only_issue: int | None = None) -> None:
    """The full Follow pass: load -> fetch -> start new follows -> retire
    unfollowed ones -> batch-append today's timeline -> write -> render.
    Every stage is independently guarded; one stage failing never prevents
    the others, and this function itself is expected to be wrapped in
    continue-on-error by the caller (digest.py's run_pipeline / the digest
    workflow) so Follow can never take the daily digest down."""
    import render  # local import: mirrors digest.py's lazy render import

    records = load_all(followed_dir)
    issues = fetch_issues()
    if only_issue is not None:
        issues = [i for i in issues if int(i["number"]) == only_issue]
        dbg(f"follow: run -> restricted to issue #{only_issue}")

    dbg(f"follow: run -> {len(records)} existing record(s), {len(issues)} owner issue(s) in scope")

    _new_follows(records, issues, data_dir)
    _unfollow_closed(records, issues, _now())

    active = [n for n, r in records.items() if r.get("status") == "active"]
    due = [n for n in active if _needs_timeline_pass(records[n]["last_development"], today)]
    if len(due) < len(active):
        dbg(f"follow: run -> {len(active) - len(due)} active follow(s) already current as of {today}; skipping")

    for i in range(0, len(due), MAX_FOLLOWS_PER_BATCH):
        _timeline_pass(records, today, due[i : i + MAX_FOLLOWS_PER_BATCH])

    for record in records.values():
        _write_record(followed_dir, record)

    render.render_all(data_dir, docs_dir, today, followed_dir)
    dbg(f"follow: run -> done, {ground._CALLS} grounded call(s) this run")

    tracer.count(
        follow_records=len(records),
        follow_issues_in_scope=len(issues),
        follow_active=len(active),
        follow_due=len(due),
        follow_grounded_calls=ground._CALLS,
    )
    tracer.artifact_json(
        "follow/records.json",
        {
            "today": f"{today:%Y-%m-%d}",
            "only_issue": only_issue,
            "grounded_calls": ground._CALLS,
            "records": [
                {
                    "issue": r.get("issue"),
                    "status": r.get("status"),
                    "headline": r.get("headline"),
                    "last_development": r.get("last_development"),
                    "timeline_entries": len(r.get("timeline") or []),
                    "due_this_run": r.get("issue") in due,
                }
                for r in records.values()
            ],
        },
    )
