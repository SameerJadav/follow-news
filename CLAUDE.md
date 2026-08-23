# CLAUDE.md

A phone-only daily news app: GitHub Actions builds it, GitHub Pages serves it at
`https://sameerjadav.github.io/follow-news/`. Two sections (World, India)
carrying only the genuinely biggest stories, written from claims individually
anchored to sources. An opt-in **Follow** researches one story's backstory and
appends a timeline entry a day until it closes. Free tiers only, no server, no
database — `data/` and `followed/` on disk are the entire backend.

It is built to run unattended for a year with nobody coming back to patch it, so
every fact here is **measured**: dated, attributed to the probe or the run that
produced it, and written down beside the code it constrains. Read the module
docstring and a constant's inline comment before changing either — the trap you
are about to hit is usually named there, with the date it was measured. This file
carries only what reading the code cannot tell you.

## The specification

- **`product.md`, `research.md`, `decisions.md`** are settled. Do not edit them
  and do not silently contradict them; a change that needs to deviate stops and
  says so instead of guessing.
- **`dossier.md`** specifies Follow's deep research, additive to those three;
  its §14 records the deviations it was signed off with.
- **`calibration.md`** is the dated log of every dial turned and every surprise
  measured. Turning a dial means adding an entry here. Its "starting dial
  values" table is a snapshot from 2026-07-27 and has since gone stale — the
  constant in the code is the value; this file is the story of why it moved.
- **`meta-plan.md`** is the build plan, now history. **`ANALYSIS-2026-07-31.md`**
  audits the live record as of that date.

## The map

Digest, in call order — `digest.py run_pipeline` orchestrates and owns the
`data/` contract: `feeds.py` (fetch, window, dedupe, per-feed health, quorum) →
`rank.py` (pool shaping, scoring, cutoff, scope; no I/O, fully unit-testable) +
`wikipedia.py` (Current Events cross-check against volume bias) → `llm.py`
(select, claims, write) → `extract.py` (article text; JSON-LD or paragraphs,
longest wins, escalating to `r.jina.ai`) → `anchor.py` (the semantic gate:
markers, anchoring floors, thin sourcing) → `render.py` (every page, from Python
template strings).

Follow: `follow.py` (issues, the owner guard, `record.json`, the sweep) →
`dossier.py` (ledger, question frontier, gap detectors, saturation, budgets,
checkpoints) → `ground.py` (the only module that calls Gemini for Follow).

Cross-cutting: `ratelimit.py` (429 classification, budgeted wait-and-resume),
`tracer.py` (`dbg()` and debug capture; stdlib only, imports no project module),
`report.py` (read-only reports over committed `data/`).

## Invariants

- `data/YYYY-MM-DD.json` and `followed/<issue>/` are the only sources of truth.
  Every page in `docs/` is derived and overwritten wholesale on each render —
  change `render.py` and re-render, never the HTML. The hand-written assets in
  `docs/` (`style.css`, `app.js`, `manifest.webmanifest`, the icons,
  `robots.txt`, `.nojekyll`) are untouched by the pipeline.
- **The digest is 3–4 Gemini calls per morning, total** — never one per article,
  story, or section. Select reads cheap headlines; full text is fetched only for
  what selection chose; claims and writing are each one batched call.
- **The write pass sees claims only, never article text**, so a fact that is not
  an anchored claim has no way into the prose. Two sources disagreeing on a
  figure stay two attributed claims, never one averaged number. Follow's write
  pass has the same discipline over ledger entries, and its dossier is
  append-only: entries are corrected and merged, never dropped.
- **Nothing follows itself.** Only an issue the owner deliberately opened starts
  a follow; closing it is the kill switch, whatever is left of its frontier.
- **Follow is never on the digest's critical path** (`continue-on-error: true`),
  and the digest keeps publishing if Follow fails entirely.
- **The free tier meters per model, so there are two daily pools.** Follow's
  grounded pool (`ground.GROUND_MODEL`, the only one that can use
  `google_search` on this tier) and the schema pool (`ground.SCHEMA_MODEL`,
  which is also `llm.MODEL`) are metered separately by `dossier.Budget`, and the
  schema cap leaves the digest's morning calls room. Read the ceiling off
  `aistudio.google.com/rate-limit`; a 429 is not where you learn it.
- **A Gemini call either uses a tool or gets a validated JSON schema, never
  both** — which is why searching, reading pages, and structuring what they
  returned are separate passes rather than one.
- **Degrade, don't fail.** Above `feeds.quorum_ok`'s floor a run with half its
  feeds dead still ships, logged as degraded. Below it, the run re-renders
  today's page with the honest "isn't ready yet" banner and exits 1, so the site
  stays truthful and GitHub emails the owner.
- **No silent caps.** Every ceiling that bites — a call budget, a discarded
  question, a page that could not be read — is logged and recorded.
- `debug/` and `cache/extract.json` are committed but derived and disposable;
  deleting either costs only the record. Capture is a strict no-op when
  `DIGEST_DEBUG` is off, and `tests/test_tracer.py` holds that line.

## Security — the repo is public

- `GEMINI_API_KEY` lives in Actions secrets only. It never reaches `data/`,
  `docs/`, `followed/`, or a log line.
- `follow.yml` is job-gated on owner-plus-label, and `follow.fetch_issues()`
  re-checks the author in Python so the guarantee survives another caller.
- Pass `github.event.issue.number` through `env:` — it is a GitHub-assigned
  integer. The issue *title* and *body* are attacker-controlled text on a public
  tracker: keep them out of every `run:` block and let `follow.py` read the body
  through the API. No workflow uses `pull_request_target`.
- `tests/test_security.py` asserts all four of these against the workflow files
  and the committed output, so a new workflow is checked by `pytest`, not by
  someone remembering this section.

## Changing what the digest says

Turn a dial before rewording a prompt. The dials are module-level constants,
each with an inline comment: `rank.py` (how many stories, which section, what
order), `anchor.py` (length targets and the anchoring floors), `feeds.py` (time
window, quorum, degradation), `report.py` (feed decay), `follow.py` (staleness,
new follows per run), `dossier.py` (rounds, saturation, the two daily pools, the
drift guards). Back the change with an observation logged in `calibration.md`.

A prompt change moves editorial judgement — story count, section split, scope,
register — so `decisions.md` requires showing it to the owner before it ships.

## Environment

`uv run digest.py --help` documents every subcommand and flag; `uv run pytest -q`
runs the whole suite with no network and no key. Only the full pipeline and
`follow` need `GEMINI_API_KEY` — `render`, `health`, `review`, and `debug` read
committed files. `DIGEST_DEBUG=1` (or `--debug`) captures a run's full evidence,
`DIGEST_DUMP_DIR=...` dumps raw model responses for new test fixtures, and
`DIGEST_WAIT_BUDGET_S=5` shortens the 429 wait budget for local testing.

`BASE_URL` in `digest.py` is the published site. This checkout is named
`daily-digest-new`; the repo is `follow-news`, and that name is the one that
belongs in a URL or a committed path.

## Diagnosing a morning you were not awake for

`dbg()` writes to stderr, so `gh run view <id> --log` is the first stop, and
`gh run list` finds the run. Every line is prefixed with its stage —
`QUORUM`, `DEGRADED`, `ZERO ITEMS`, `ratelimit:`, `anchor: DROPPED`,
`dossier: #<issue>` — so grep the prefix rather than reading the log.

Actions logs expire; `debug/<date>/` does not. `debug/README.md` documents the
layout, `run.json`'s `stopped_at` names the stage that emptied the pipeline, and
`uv run digest.py debug` bundles the committed runs into `debug/ANALYSIS.md`.
