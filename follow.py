"""The Follow domain: turning a prefilled GitHub issue into a followed-story
page that grows a day at a time.

`followed/<issue>/` is a source of truth alongside `data/`; every page in
docs/ that concerns a followed story is derived from it and is rebuilt
wholesale by render.py. Only this module writes `record.json`, and only
dossier.py writes `dossier.json`/`corpus.json` beside it.

    followed/<issue>/
      record.json     this module's contract, unchanged in shape
      dossier.json    the evidence: ledger, entities, frontier, chips
      corpus.json     extracted article text, keyed by URL

Legacy flat `followed/<issue>.json` records still load, so follows made before
the dossier existed keep rendering; the migration is one-way and happens the
next time a record is written.

product.md is emphatic that **nothing follows itself** — a record here exists
only because the repo owner opened an issue labelled "follow", and every
GitHub-facing function in this module re-checks that before doing anything.
The workflow-level `github.event.issue.user.login == 'SameerJadav'` guard in
follow.yml is the first gate; fetch_issues()'s filter below is the second,
so the guard holds even if this is ever invoked outside that workflow.
Closing the issue is the owner's kill switch: an unfollowed record never
resumes research, however much of its frontier is left.

Quota is no longer protected by batching one grounded call across every active
follow — dossier.md §14 replaces that with, in order: MAX_CALLS_PER_FOLLOW,
MAX_RESEARCH_CALLS_PER_DAY spent stalest-first, the saturation exit,
checkpointed resumption, and MAX_NEW_FOLLOWS_PER_RUN dropped to 1. The owner
signed that deviation off on 2026-07-27; see calibration.md.

Research happens once per follow and is never repeated. PROSE, by contrast, is
regenerable — rewriting it costs one call over an append-only ledger, which is
why the old "the backstory is never regenerated" rule is superseded (§11).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

import dossier
import extract
import ground
import tracer
from tracer import dbg
from rank import SECTIONS

OWNER = "SameerJadav"
REPO = "SameerJadav/follow-news"
FOLLOW_LABEL = "follow"
BASE_URL = "https://sameerjadav.github.io/follow-news/"

STALE_DAYS = 14  # decisions.md: auto-close after ~14 days with no development
# dossier.md §14: was 3. A new follow now costs a burst of research calls
# rather than one, so a burst of REQUESTS must not stack bursts of research in
# one morning. The rest wait for the next run; they stay open and unrecorded.
MAX_NEW_FOLLOWS_PER_RUN = 1

_API = "https://api.github.com"
_REQUEST_TIMEOUT = 15

_FIELD_RE = re.compile(r"^\s*(digest|section|story|headline)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS_RE = re.compile(r"\s+")

# ---------- the followed/ contract ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_label(d: date) -> str:
    return d.strftime("%A, %-d %B %Y")


def load_all(followed_dir: Path) -> dict[int, dict]:
    """Every followed record, keyed by issue number. Returns {} if the
    directory doesn't exist yet — Follow has never run.

    Reads followed/<issue>/record.json first, then legacy flat
    followed/<issue>.json for any issue without a directory. A directory
    always wins: it is the migrated copy, and a flat file left beside it is
    a stale remnant of a half-finished write, never newer.

    Note the glob is `*/record.json`, not `*.json` — a plain `*.json` glob is
    NON-RECURSIVE and would silently miss every new-style record, which would
    look exactly like "this follow has no record yet" and reseed its research
    from scratch on every single run."""
    if not followed_dir.exists():
        return {}

    records: dict[int, dict] = {}
    for path in sorted(followed_dir.glob("*/record.json"), key=lambda p: p.parent.name):
        try:
            record = json.loads(path.read_text())
            records[int(record["issue"])] = record
        except (OSError, ValueError, KeyError) as exc:
            dbg(f"follow: could not load {path} ({exc!r}); skipping")

    for path in sorted(followed_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
            n = int(record["issue"])
        except (OSError, ValueError, KeyError) as exc:
            dbg(f"follow: could not load {path} ({exc!r}); skipping")
            continue
        if n in records:
            dbg(f"follow: #{n} has both a directory and a legacy file; the directory wins")
            continue
        records[n] = record
    return records


def _write_record(followed_dir: Path, record: dict) -> None:
    """Write followed/<issue>/record.json, migrating a legacy flat file into
    the directory on the way. One-way and lazy: nothing rewrites a record
    that is never touched again."""
    issue = record["issue"]
    d = followed_dir / str(issue)
    d.mkdir(parents=True, exist_ok=True)
    (d / "record.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    dbg(f"follow: wrote {d / 'record.json'}")

    legacy = followed_dir / f"{issue}.json"
    if legacy.exists():
        try:
            legacy.unlink()
            dbg(f"follow: migrated legacy {legacy} into {d}/")
        except OSError as exc:
            dbg(f"follow: could not remove legacy {legacy} ({exc!r})")


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


def _new_follows(
    records: dict[int, dict], issues: list[dict], data_dir: Path, followed_dir: Path
) -> None:
    """Register new follows and seed their dossiers. Pass A only — the
    research loop itself runs later in the sweep, under the shared budget.

    The record and its dossier are created in the SAME step, deliberately:
    render.py reads a record with no dossier.json beside it as a legacy
    one-shot follow and shows its (empty) prose as finished. A window where
    record.json exists and dossier.json does not would publish an empty page."""
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

        now = _now()
        origin = {
            "date": resolved["date"],
            "section": resolved["section"],
            "position": resolved["position"],
            "headline": resolved["headline"],
        }
        record = {
            "issue": n,
            "status": "active",
            "title": resolved["headline"],
            "section": resolved["section"],
            "origin": origin,
            "started_at": _iso(now),
            "closed_at": None,
            "close_reason": None,
            "last_development": resolved["date"],
            "backstory": {},
            "timeline": [],
        }

        try:
            dsr, corpus = dossier.seed(followed_dir, data_dir, n, origin, resolved["headline"])
        except Exception as exc:  # noqa: BLE001 - Pass A is free; a failure must not lose the follow
            dbg(f"follow: #{n} -> Pass A failed ({exc!r}); leaving open for a later run")
            continue

        records[n] = record
        _write_record(followed_dir, record)
        dossier.save(followed_dir, n, dsr, corpus, "A")

        # Acknowledge NOW, not after the research burst. A deep follow can take
        # tens of minutes; the old single call took twelve seconds, and silence
        # for half an hour reads as a broken button.
        comment(n, f"Following — researching this story now. It will appear at {BASE_URL}follow-{n}.html")
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
    calibrate STALE_DAYS without touching the update pass, and so it can be
    tested without a network call."""
    last_dev = datetime.strptime(last_development, "%Y-%m-%d").date()
    return (today - last_dev).days >= STALE_DAYS


def _sweep(
    records: dict[int, dict],
    followed_dir: Path,
    today: date,
    budget: dossier.Budget,
) -> list[int]:
    """Spend the day's research budget across active follows, stalest first.

    Order matters and is a real choice: a page stuck saying "researching this
    story" is worse for a reader than a delayed one-line update, so unfinished
    research goes before daily updates. With MAX_CALLS_PER_FOLLOW equal to
    MAX_RESEARCH_CALLS_PER_DAY, one new follow can legitimately consume the
    whole day and defer everything else — logged, never silent.

    Returns the issues whose prose changed, so run() knows what to re-render.
    """
    active = [n for n, r in records.items() if r.get("status") == "active"]

    pending, due = [], []
    for n in active:
        dsr, _ = dossier.load(followed_dir, n)
        if dsr is None:
            continue  # legacy follow, no dossier: nothing to research
        if dossier.needs_research(dsr):
            pending.append((str(dsr.get("checkpoint", {}).get("updated_at") or ""), n))
        elif _needs_timeline_pass(records[n]["last_development"], today):
            due.append((records[n]["last_development"], n))

    pending.sort()
    due.sort()
    order = [n for _, n in pending] + [n for _, n in due]
    if not order:
        dbg(f"follow: sweep -> nothing to research; {len(active)} active follow(s) all current")
        return []

    dbg(f"follow: sweep -> {len(pending)} researching, {len(due)} due; budget {budget.remaining()}")
    touched: list[int] = []

    for n in order:
        if budget.day_quota_hit:
            budget.defer(n, "daily_quota")
            continue
        if budget.remaining() <= 0:
            budget.defer(n, "day_budget")
            continue

        dsr, corpus = dossier.load(followed_dir, n)
        if dsr is None:
            continue
        record = records[n]

        try:
            if dossier.needs_research(dsr):
                state = dossier.research(followed_dir, n, dsr, corpus, budget)
                if state in ("complete", "capped"):
                    _write_picture(followed_dir, n, record, dsr, corpus, budget)
                    touched.append(n)
                dossier.save(followed_dir, n, dsr, corpus, "DONE")
            elif not (record.get("backstory") or {}).get("body"):
                # Research finished but the prose did not — the write pass was
                # interrupted, or every group failed its gate. Retry the WRITE,
                # never fall through to the update pass: an update would append
                # a timeline entry and leave "The full picture" empty forever,
                # on a story whose ledger is already complete.
                dbg(f"follow: #{n} -> research done but no prose; retrying the write pass")
                _write_picture(followed_dir, n, record, dsr, corpus, budget)
                dossier.save(followed_dir, n, dsr, corpus, "DONE")
                touched.append(n)
            else:
                _update_follow(followed_dir, n, record, dsr, corpus, today, budget)
                touched.append(n)
        except dossier.DailyQuotaExhausted:
            # Account-wide, so every remaining follow would fail identically.
            dbg("follow: sweep -> daily quota exhausted; stopping the sweep")
            dossier.save(followed_dir, n, dsr, corpus, dsr["checkpoint"]["stage"])
            break
        except Exception as exc:  # noqa: BLE001 - one bad follow must not stop the rest
            dbg(f"follow: #{n} -> research/write failed ({exc!r}); checkpointed, will resume")
            dossier.save(followed_dir, n, dsr, corpus, dsr["checkpoint"]["stage"])

    return touched


def _write_picture(
    followed_dir: Path,
    n: int,
    record: dict,
    dsr: dict,
    corpus: dict,
    budget: dossier.Budget,
) -> None:
    """Write (or rewrite) a followed story's full picture from its ledger.

    Prose is regenerable and research is not, so this is safe to retry: it
    costs one call per write group over a ledger that only ever grows."""
    block = dossier.write_backstory(followed_dir, n, dsr, corpus, budget)
    if block is None:
        dbg(f"follow: #{n} -> write produced nothing publishable; the ledger is kept")
        return
    record["backstory"] = {**block, "generated_at": _iso(_now())}
    dsr["written_through"] = dsr["rounds"]


def _update_follow(
    followed_dir: Path,
    n: int,
    record: dict,
    dsr: dict,
    corpus: dict,
    today: date,
    budget: dossier.Budget,
) -> None:
    """One day's update for an already-researched follow: a short research
    round, then prose covering only what it added.

    "Quiet" is mechanical now — zero new ledger entries — rather than a model
    judgement about whether today felt quiet."""
    before = len(dsr["ledger"])
    dsr["research_state"] = "researching"
    dsr["rounds"] = max(0, dsr["rounds"])
    dossier.admit(
        dsr["questions"],
        [
            dossier._question(
                f"What has happened in this story since {record['last_development']}?",
                origin="gap",
                score=0.95,
            )
        ],
    )
    # One round only: a daily update is a delta, not a re-research.
    saved_max, dossier.MAX_ROUNDS = dossier.MAX_ROUNDS, dsr["rounds"] + 1
    try:
        dossier.research(followed_dir, n, dsr, corpus, budget)
    finally:
        dossier.MAX_ROUNDS = saved_max

    if len(dsr["ledger"]) == before:
        dbg(f"follow: #{n} -> quiet, no new ledger entries")
        dsr["research_state"] = "complete"
        dossier.save(followed_dir, n, dsr, corpus, "DONE")
        return

    block = dossier.write_update(followed_dir, n, dsr, corpus, budget)
    dsr["research_state"] = "complete"
    if block is None:
        dossier.save(followed_dir, n, dsr, corpus, "DONE")
        return

    now = _now()
    is_closing = _is_closing(record["last_development"], today)
    entry = {**block, "generated_at": _iso(now)}
    entry["date"] = today.isoformat()
    entry["date_label"] = _date_label(today)
    entry["kind"] = "final" if is_closing else "development"
    record["timeline"] = (record.get("timeline") or []) + [entry]
    record["last_development"] = today.isoformat()
    dsr["written_through"] = dsr["rounds"]
    dossier.save(followed_dir, n, dsr, corpus, "DONE")

    if is_closing:
        record["status"] = "closed"
        record["close_reason"] = "no_development"
        record["closed_at"] = _iso(now)
        close_issue(n, f"No new developments for {STALE_DAYS} days — closing. Final update: {BASE_URL}follow-{n}.html")
        dbg(f"follow: #{n} -> closed, no development for {STALE_DAYS}+ days")


def run(data_dir: Path, followed_dir: Path, docs_dir: Path, today: date, only_issue: int | None = None) -> None:
    """The full Follow pass: load -> fetch -> start new follows -> retire
    unfollowed ones -> research and write under the day's budget -> render.
    Every stage is independently guarded; one stage failing never prevents
    the others, and this function itself is expected to be wrapped in
    continue-on-error by the caller (digest.py's run_pipeline / the digest
    workflow) so Follow can never take the daily digest down."""
    import render  # local import: mirrors digest.py's lazy render import

    # The extraction cache is shared across follows and across days: fourteen
    # days of updates on one story would otherwise re-fetch the same background
    # articles every morning, each costing a request and a Jina pause.
    extract.enable_cache(followed_dir.parent / "cache" / "extract.json")

    records = load_all(followed_dir)
    issues = fetch_issues()
    if only_issue is not None:
        issues = [i for i in issues if int(i["number"]) == only_issue]
        dbg(f"follow: run -> restricted to issue #{only_issue}")

    dbg(f"follow: run -> {len(records)} existing record(s), {len(issues)} owner issue(s) in scope")

    _new_follows(records, issues, data_dir, followed_dir)
    _unfollow_closed(records, issues, _now())

    budget = dossier.load_budget(followed_dir, today)
    active = [n for n, r in records.items() if r.get("status") == "active"]
    due = _sweep(records, followed_dir, today, budget)

    for record in records.values():
        _write_record(followed_dir, record)

    render.render_all(data_dir, docs_dir, today, followed_dir)
    dbg(f"follow: run -> done, {ground._CALLS} grounded call(s) this run")

    tracer.count(
        follow_records=len(records),
        follow_issues_in_scope=len(issues),
        follow_active=len(active),
        follow_touched=len(due),
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
                    "touched_this_run": r.get("issue") in due,
                }
                for r in records.values()
            ],
        },
    )
