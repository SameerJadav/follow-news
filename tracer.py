"""Run-scoped debug capture — the evidence a morning leaves behind.

`data/YYYY-MM-DD.json` records what the pipeline *published*. This module
records everything it *rejected*, and why: the feeds that came back empty,
the clusters ranking cut, the pages the scraper failed to read, the exact
prompt each Gemini call saw, the raw model output behind a story anchor.py
threw away. That is the half you need to debug a morning you weren't awake
for, and none of it survives today beyond a stderr line in an Actions log.

Everything here is off by default and a hard no-op when off — no directory
is created, no file is written, nothing is formatted. `DIGEST_DEBUG=1` (set
in digest.yml for the Phase 6 Part B calibration window) turns it on;
setting it to 0 there turns the whole apparatus off again without touching
a line of pipeline code.

Two rules this module enforces on behalf of its callers:

- **Never a secret.** `scrub()` runs over every artifact body and every
  event field before it touches disk. The repo is public and `debug/` is
  committed, so this is the last line of defence and it lives here rather
  than at each of the ~40 call sites.
- **Never a silent truncation.** An artifact over MAX_ARTIFACT_BYTES, or a
  run over MAX_RUN_BYTES, gets a visible footer and a `tracer` event saying
  so. A debug bundle that quietly dropped the one page you needed is worse
  than no bundle at all.

Stdlib only, and imports no project module — `feeds` imports `dbg` from
here, so anything imported here would be a cycle.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEBUG_DIR = Path(__file__).resolve().parent / "debug"

# A single page of HTML is ~300KB; 2MB is generous enough that nothing real
# is ever cut, and low enough that one pathological page can't dominate the
# commit. Raw HTML is stored uncompressed on purpose: git zlib-compresses
# blobs in its own packfiles, so gzipping first would save almost nothing in
# the repo while making every file unreadable to whoever analyses it later.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_RUN_BYTES = 64 * 1024 * 1024

# Env vars safe to record in run.json. Anything not on this list is omitted
# rather than redacted — an allowlist can't be defeated by a new secret
# showing up in the environment under a name nobody thought to blocklist.
_ENV_ALLOWLIST = (
    "DIGEST_DEBUG",
    "DIGEST_WAIT_BUDGET_S",
    "DIGEST_DUMP_DIR",
    "CI",
    "GITHUB_ACTIONS",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_WORKFLOW",
    "GITHUB_EVENT_NAME",
    "GITHUB_SHA",
    "GITHUB_REF",
    "RUNNER_OS",
)

# Shapes, not values: catches a key that arrived from somewhere other than
# the environment we can read (a pasted prompt, a model echoing its input).
_SECRET_SHAPES = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
)

_SECRET_ENV = ("GEMINI_API_KEY", "GITHUB_TOKEN")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _level_from_env() -> int:
    raw = os.environ.get("DIGEST_DEBUG", "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return 0
    if raw in ("1", "true", "yes", "on"):
        return 1
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


LEVEL = _level_from_env()


def enabled() -> bool:
    return LEVEL > 0


def configure(level: int | None) -> None:
    """Override the env-derived level (digest.py's --debug/--no-debug).
    Called before start(); has no effect on a run already under way."""
    global LEVEL
    if level is not None:
        LEVEL = max(0, level)


def slug(text: str, limit: int = 60) -> str:
    """Filesystem-safe fragment of an outlet name or URL, for artifact names
    that a human can scan without opening them."""
    out = _SLUG_RE.sub("-", str(text).lower()).strip("-")
    return (out[:limit].rstrip("-")) or "unnamed"


def scrub(text: str) -> str:
    """Redact anything key-shaped, plus the live values of the two secrets
    we know we hold. Cheap, and the only thing standing between a debug dump
    and a public repo."""
    if not text:
        return text
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, f"[REDACTED:{name}]")
    for shape in _SECRET_SHAPES:
        text = shape.sub("[REDACTED:key-shaped]", text)
    return text


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


class _Run:
    """One pipeline invocation's capture state. Everything mutable lives
    here so a no-op run allocates nothing."""

    def __init__(self, kind: str, day: date, root: Path) -> None:
        self.kind = kind
        self.day = day
        self.root = root
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self.bytes_written = 0
        self.capped = False
        self.stages: list[dict[str, Any]] = []
        self.funnel: dict[str, Any] = {}
        self.extras: dict[str, Any] = {}
        self.trace = (root / "trace.jsonl").open("a", encoding="utf-8")

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


_RUN: _Run | None = None


def dbg(msg: str) -> None:
    """Diagnostics to stderr — never into the site. A scheduled run at 02:00
    IST can only be debugged afterwards, so this is unconditional and always
    has been. When capture is on it also lands in trace.jsonl, which is what
    makes the timeline readable next to the artifacts it explains."""
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", file=sys.stderr, flush=True)
    if _RUN is not None:
        _write_line({"t": _RUN.elapsed_ms(), "kind": "dbg", "msg": scrub(msg)})


def _write_line(payload: dict[str, Any]) -> None:
    run = _RUN
    if run is None:
        return
    try:
        run.trace.write(json.dumps({"run": run.kind, **payload}, ensure_ascii=False, default=str) + "\n")
        run.trace.flush()
    except Exception:  # noqa: BLE001 - capture must never break the pipeline
        pass


def start(kind: str, day: date) -> None:
    """Open debug/<day>/ for this run. Safe to call twice (the second call
    is ignored) so a `follow` run inside a digest run doesn't reopen it."""
    global _RUN
    if not enabled() or _RUN is not None:
        return
    try:
        root = DEBUG_DIR / f"{day:%Y-%m-%d}"
        root.mkdir(parents=True, exist_ok=True)
        _RUN = _Run(kind, day, root)
    except Exception as exc:  # noqa: BLE001
        print(f"[tracer] could not open debug dir: {exc!r}", file=sys.stderr, flush=True)
        return
    event("tracer", msg="capture started", kind=kind, level=LEVEL, day=f"{day:%Y-%m-%d}")


def event(stage: str, /, **fields: Any) -> None:
    """One structured row on the timeline. Use for anything with numbers in
    it — a decision, a threshold comparison, a timing.

    `stage` is positional-only so a caller can still pass a field literally
    named "stage" without colliding with it."""
    if _RUN is None:
        return
    _write_line(
        {
            "t": _RUN.elapsed_ms(),
            "kind": "event",
            "stage": stage,
            **{k: _scrub_value(v) for k, v in fields.items()},
        }
    )


def count(**kw: int) -> None:
    """Funnel counters. Merged across calls, so a stage can report its own
    numbers without knowing what anything else reported."""
    if _RUN is None:
        return
    _RUN.funnel.update(kw)


def extra(key: str, value: Any) -> None:
    """Attach a named block to run.json (e.g. the resolved dial values)."""
    if _RUN is None:
        return
    _RUN.extras[key] = _scrub_value(value)


@contextmanager
def stage(name: str):
    """Time a pipeline stage and record whether it raised. The exception is
    re-raised untouched — this observes, it never handles.

    A stage that RAISED also names itself in `stopped_at`, the field run.json
    is documented to answer "which stage emptied the pipeline?" with. Before
    2026-08-23 only digest.stopped() wrote it, so an exception — the 503 that
    lost 2026-08-07, the one day it was ever needed — bypassed it entirely and
    left the field absent. setdefault, so the innermost stage that failed is
    the one recorded and an outer frame cannot overwrite it."""
    if _RUN is None:
        yield
        return
    started = time.monotonic()
    error = ""
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        error = repr(exc)
        raise
    finally:
        ms = int((time.monotonic() - started) * 1000)
        if _RUN is not None:
            _RUN.stages.append({"stage": name, "ms": ms, "error": scrub(error)})
            if error:
                _RUN.extras.setdefault("stopped_at", name)
                _RUN.extras.setdefault("stopped_error", scrub(error))
            event("stage", name=name, ms=ms, ok=not error, error=scrub(error))


def _namespaced(relpath: str, kind: str) -> str:
    """`relpath` with the run kind folded into the FILE NAME for any run that
    is not the digest — extract/index.json becomes extract/index-follow.json.

    digest.yml runs `digest.py follow` as a second process against the same
    debug/<day>/, and finish() has always suffixed run.json/funnel.json for
    exactly that reason. artifact() did not, so a follow run that extracted
    background articles silently overwrote the digest's own extract/index.json:
    measured on 2026-07-30, 49 extractions in the funnel against 4 rows left in
    the index (ANALYSIS-2026-08-23.md §M3)."""
    if kind == "digest":
        return relpath
    head, _, tail = relpath.rpartition("/")
    name, dot, ext = tail.partition(".")
    tail = f"{name}-{kind}{dot}{ext}"
    return f"{head}/{tail}" if head else tail


def artifact(relpath: str, content: str | bytes) -> str | None:
    """Write one file under debug/<day>/. Returns the relative path actually
    written (a non-digest run's is namespaced — see _namespaced) so the caller
    can reference it from an index, or None if nothing was written."""
    run = _RUN
    if run is None:
        return None
    relpath = _namespaced(relpath, run.kind)
    try:
        raw = content.encode("utf-8", "replace") if isinstance(content, str) else bytes(content)
        # Scrub as text when we can; binary feed bytes are left alone beyond
        # a shape check, since a secret has no way into an RSS body.
        try:
            raw = scrub(raw.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            pass

        note = b""
        if len(raw) > MAX_ARTIFACT_BYTES:
            note = f"\n\n[tracer: truncated at {MAX_ARTIFACT_BYTES} bytes of {len(raw)}]\n".encode()
            event("tracer", msg="artifact truncated", path=relpath, bytes=len(raw), cap=MAX_ARTIFACT_BYTES)
            raw = raw[:MAX_ARTIFACT_BYTES]

        if run.bytes_written + len(raw) > MAX_RUN_BYTES:
            if not run.capped:
                run.capped = True
                event("tracer", msg="RUN BYTE CAP REACHED — further artifacts skipped", cap=MAX_RUN_BYTES)
            return None

        path = run.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw + note)
        run.bytes_written += len(raw)
        return relpath
    except Exception as exc:  # noqa: BLE001 - capture must never break the pipeline
        event("tracer", msg="artifact write failed", path=relpath, error=repr(exc))
        return None


def artifact_json(relpath: str, payload: Any) -> str | None:
    """artifact() for structured data — the common case for index files."""
    if _RUN is None:
        return None
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        event("tracer", msg="artifact json failed", path=relpath, error=repr(exc))
        return None
    return artifact(relpath, text)


def _git_sha() -> str:
    """Read HEAD without shelling out — this runs inside the pipeline and
    must not spawn a process or depend on git being on PATH."""
    try:
        git = Path(__file__).resolve().parent / ".git"
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = git / head[5:]
            if ref.exists():
                return ref.read_text().strip()
            for line in (git / "packed-refs").read_text().splitlines():
                if line.endswith(" " + head[5:]):
                    return line.split()[0]
            return ""
        return head
    except Exception:  # noqa: BLE001
        return ""


def dials() -> dict[str, Any]:
    """Every calibration dial's value at run time, so a debug bundle read a
    month later is interpretable against the code that produced it rather
    than against whatever the dials have since been tuned to."""
    out: dict[str, Any] = {}
    wanted = {
        "rank": ("WEIGHT_FLOOR", "TIER_WEIGHT", "WIKI_BONUS", "HARD_CAP", "MIN_STORIES_IF_ANY",
                 "MAX_ARTICLES_PER_STORY", "MAX_PER_OUTLET", "POOL_CAP"),
        "anchor": ("WORD_TARGET", "THIN_MIN_CLAIM_OUTLETS", "THIN_MAX_OUTLET_SHARE",
                   "MAX_UNANCHORED_SHARE", "MIN_MARKERS", "MIN_BODY_WORDS",
                   "MIN_FULLTEXT_CHARS", "MAX_CLAIMS_PER_STORY", "MIN_CLAIM_CHARS"),
        "feeds": ("WINDOW_FLOOR_H", "WINDOW_CAP_H", "MIN_LIVE_FEEDS", "MIN_ARTICLES",
                  "DEGRADED_LIVE_SHARE", "SUMMARY_CAP"),
        "extract": ("MIN_CHARS", "ARTICLE_CAP", "PARA_MIN", "JINA_PAUSE"),
        "report": ("DEAD_DAYS", "HISTORY_DAYS"),
        "follow": ("STALE_DAYS", "MAX_NEW_FOLLOWS_PER_RUN"),
        "dossier": ("QUESTIONS_PER_ROUND", "QUESTIONS_PER_CALL", "MAX_ROUNDS",
                    "SATURATION_ENTRIES", "SATURATION_ROUNDS", "MAX_CALLS_PER_FOLLOW",
                    "MAX_GROUNDED_CALLS_PER_DAY", "MAX_SCHEMA_CALLS_PER_DAY",
                    "QUESTIONS_PER_CALL", "CRITIC_EVERY",
                    "MAX_QUESTION_DEPTH", "MIN_QUESTION_SCORE",
                    "MAX_URLS_PER_CONTEXT_CALL", "MAX_FETCH_PER_ROUND", "PHASED_WRITE_ENTRIES",
                    "GAP_DENSITY_RATIO", "MIN_ENTRY_COVERAGE", "MERGE_SIMILARITY"),
        "ratelimit": ("WAIT_BUDGET_S",),
        "llm": ("MODEL", "MAX_OUTPUT_TOKENS", "MAX_JSON_RETRIES"),
        "ground": ("GROUND_MODEL", "SCHEMA_MODEL", "MAX_OUTPUT_TOKENS", "MAX_OUTPUT_TOKENS_LONG"),
    }
    for mod_name, names in wanted.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for name in names:
            if hasattr(mod, name):
                out[f"{mod_name}.{name}"] = getattr(mod, name)
    return out


def finish(ok: bool) -> None:
    """Close the run: write run.json and funnel.json. Must be reached on
    every exit path, especially the failing ones — a morning that broke is
    exactly the morning whose evidence matters."""
    global _RUN
    run = _RUN
    if run is None:
        return
    try:
        event("tracer", msg="capture finished", ok=ok, ms=run.elapsed_ms(), bytes=run.bytes_written)
        payload = {
            "kind": run.kind,
            "day": f"{run.day:%Y-%m-%d}",
            "ok": ok,
            "started_at": run.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_ms": run.elapsed_ms(),
            "argv": [scrub(a) for a in sys.argv],
            "python": sys.version.split()[0],
            "git_sha": _git_sha(),
            "debug_level": LEVEL,
            "bytes_written": run.bytes_written,
            "byte_cap_reached": run.capped,
            "env": {k: scrub(os.environ[k]) for k in _ENV_ALLOWLIST if k in os.environ},
            "dials": dials(),
            "stages": run.stages,
            **run.extras,
        }
        # digest.yml runs `digest.py follow` as a second process against the
        # same day, so these must not collide: the digest's own evidence
        # would otherwise be overwritten by the Follow step that ran after
        # it. trace.jsonl is shared on purpose (opened append, every line
        # tagged with its run kind) — one chronological timeline is right.
        suffix = "" if run.kind == "digest" else f"-{run.kind}"
        (run.root / f"run{suffix}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
        )
        (run.root / f"funnel{suffix}.json").write_text(
            json.dumps(run.funnel, indent=2, ensure_ascii=False, default=str) + "\n"
        )
        run.trace.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[tracer] finish failed: {exc!r}", file=sys.stderr, flush=True)
    finally:
        _RUN = None
