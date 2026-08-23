# debug/

Evidence captured from pipeline runs. **Derived and disposable** — nothing
here is a source of truth, nothing reads it back into the pipeline, and
deleting the whole directory costs only the record.

It exists for the Phase 6 Part B calibration window (`calibration.md`: seven
consecutive correct mornings before the project is done). `data/` records
what the digest *published*; this records what it *rejected*, and why.

## Reading it

```sh
uv run digest.py debug            # bundle every captured day into ANALYSIS.md
uv run digest.py debug --days 3   # just the last three
```

**Start with `ANALYSIS.md`.** It is generated, self-contained, and explains
what each pipeline stage does before showing what happened at it — hand that
one file to someone (or something) that has never seen this repo. Everything
below is the backing evidence it points at.

## The switch

Capture is **off by default in code**. `.github/workflows/digest.yml` sets
`DIGEST_DEBUG: "1"` at workflow level; setting it to `"0"` there switches
everything off — no directory created, no file written, no other change.
Locally: `DIGEST_DEBUG=1 uv run digest.py`, or `--debug` / `--no-debug`.

## Layout

One directory per day, `debug/YYYY-MM-DD/`:

| path | what it holds |
|------|---------------|
| `run.json` | outcome, `stopped_at`, stage timings, every dial's value, git sha, whitelisted env |
| `funnel.json` | counts in and out of every stage |
| `trace.jsonl` | one chronological timeline: every `dbg()` line and every structured event, each tagged with its run |
| `feeds/index.json` | per-feed health, plus every article dropped and the reason |
| `feeds/*.xml` | raw RSS bytes as served |
| `pool.json` | the exact article pool the select prompt saw, with the indices it used |
| `wikipedia/*.wikitext` | raw Current Events source |
| `wikipedia/events.json` | parsed events and the prompt block built from them |
| `wikipedia/coverage.json` | curated events no story covered |
| `rank.json` | every cluster considered: weight arithmetic, verdict, why it was cut |
| `extract/index.json` | per article: HTTP status, each strategy's yield, which won, final length |
| `extract/NNN-*.html` | the raw page fetched |
| `extract/NNN-*.txt` | the text actually handed to the claims pass |
| `extract/NNN-*.jina.txt` | the reader-proxy body, when it was used |
| `llm/N-*.prompt.txt` | the full prompt sent |
| `llm/N-*.system.txt` | the system instruction |
| `llm/N-*.response.json` | the raw model response |
| `llm/N-*.meta.json` | model, latency, token counts, finish reason, retries |
| `claims.json` | claims per cluster, and whether each drew on full text or an RSS summary |
| `anchor/index.json` | every story judged, its metrics against each threshold |
| `anchor/dropped-*.json` | full model output and context for a story that was thrown away |
| `render.json` | pages written and their byte sizes |
| `follow/` | grounded prompts, responses, resolved redirect URLs |
| `dossier/<issue>/index.json` | a followed story's research: rounds, calls, ledger size, entity sides, gap firings, what it could not read |
| `dossier/<issue>/discarded-questions.json` | every question the drift guards cut, and which guard cut it |

A `follow` run against the same day folds `-follow` into **every** file name
it writes — `run-follow.json`, `funnel-follow.json`, `extract/index-follow.json`,
`dossier/<issue>/index-follow.json` — so it can never overwrite the digest's own
record. It did overwrite `extract/index.json` until 2026-08-23
(`ANALYSIS-2026-08-23.md` §M3). Both runs append to the shared `trace.jsonl`,
each line tagged with its run kind; one chronological timeline is right.

Retention: the commit step prunes `debug/<date>/` older than `report.RETAIN_DAYS`
before committing. At 17 MB a day, unbounded capture was 89% of the objects in a
checkout the three daily crons each pay for (§H5); the retained window is what a
morning is actually diagnosed from, and git history keeps the rest.

## Two things to know

- **The repo is public.** `debug/` carries full article text from BBC,
  Guardian, Times of India and the rest, plus every prompt. `tracer.scrub()`
  redacts anything key-shaped before writing, and `tests/test_security.py`
  checks that it held. Removing the directory later clears the working tree
  but **not git history**.
- **Raw HTML is stored uncompressed.** Git zlib-compresses blobs in its own
  packfiles, so gzipping first would save almost nothing in the repo while
  making every file unreadable to whoever is analysing it.
