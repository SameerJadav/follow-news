# Calibration log

Phase 6 Part B: the digest must run unattended and correctly for at least
seven consecutive mornings before this project is considered done. This file
is the evidence trail for that — one dated entry per morning, read from the
actual page and from `uv run digest.py review`, not from memory or intuition.

Dials referenced below live in `rank.py`, `anchor.py`, `feeds.py`, and
`report.py` — see `CLAUDE.md`'s "Calibration dials" table for the full list.
Any change to an `llm.py` prompt that moves editorial judgement (story
count, section split, scope, register) gets shown to the owner before it
ships — a dial gets tuned directly, a prompt does not.

**Morning checklist:**

1. `git pull`
2. `gh run list --workflow digest.yml --limit 3` — confirm the scheduled run fired and is green.
3. `gh run view <id> --log | grep -E "QUORUM|DEGRADED|ZERO ITEMS|ratelimit|FATAL|NOT COVERED"`
4. `uv run digest.py review` — the evidence.
5. `uv run digest.py debug` — the fuller evidence, including everything the
   run *rejected*: clusters the ranker cut, pages the scraper couldn't read,
   stories the anchoring gate dropped. `git pull` already brought
   `debug/<date>/` down with the digest. On a red morning, `stopped_at` in
   `debug/<date>/run.json` names the stage that emptied the pipeline.
6. Read the actual page on the phone.
7. Append an entry below in this shape:

```
## YYYY-MM-DD
run: green|red, N/14 feeds, N LLM calls
count: N stories (world N, india N) — felt right / thin / bloated
missed: <anything from "wiki: NOT COVERED" that genuinely mattered, or "nothing">
split: <did India-angle-wins hold?>
prose: <reads as writing, or stapled facts? which story was worst, if any?>
vocab: <right words, or obvious ones?>
changed: <dial + old -> new, or "nothing">
```

---

## Deviation: `decisions.md:70` — "No cap on active follows" (2026-07-27)

`decisions.md:70` reads: *"**No cap** on active follows; quota is protected by
batching."* Batching is no longer the protection.

`dossier.md` replaces Follow's single-call backstory with multi-round research,
and §12 gives each active follow its own daily research instead of one call
batched across all of them. Recurring cost now scales with the number of
follows: five active follows at two calls each is ten grounded calls a morning
on top of the digest's three.

The protection mechanism becomes, in order:

1. `MAX_CALLS_PER_FOLLOW` — the one-time research burst is bounded
2. `MAX_RESEARCH_CALLS_PER_DAY` — recurring cost across all follows, spent
   stalest-first, with every deferral recorded in
   `followed/_budget/<date>.json`
3. the saturation exit (§7) — most stories stop well below the ceilings
4. checkpointed resumption (§9) — exceeding a budget delays, never fails
5. `MAX_NEW_FOLLOWS_PER_RUN` dropped from 3 to 1

Owner signed this off on 2026-07-27. `decisions.md` itself stays settled and
unedited, per `CLAUDE.md`.

**Consequence to watch on the first live run.** `MAX_CALLS_PER_FOLLOW` and
`MAX_RESEARCH_CALLS_PER_DAY` are both 40, so one new follow can legitimately
consume the whole day's pool and defer every other follow's update. `_sweep`
orders unfinished research ahead of daily updates deliberately — a page stuck
saying "researching this story" is worse for a reader than a delayed one-line
update — but that is a choice, not something the dial values settle. If a
morning ever defers an update that mattered, the fix is to lower
`MAX_CALLS_PER_FOLLOW` below the daily cap, not to reorder the sweep.

## Starting dial values (2026-07-27, uncalibrated)

`dossier.md` §15 deliberately left these unset; they are the first thing to
tune against real runs, from `DIGEST_DEBUG` capture.

```
QUESTIONS_PER_ROUND        = 10     QUESTIONS_PER_CALL      = 3
MAX_ROUNDS                 = 6      SATURATION_ENTRIES      = 3
SATURATION_ROUNDS          = 2      MAX_CALLS_PER_FOLLOW    = 40
MAX_RESEARCH_CALLS_PER_DAY = 40     MAX_QUESTION_DEPTH      = 3
MIN_QUESTION_SCORE         = 0.45   MAX_URLS_PER_CONTEXT_CALL = 20
MAX_FETCH_PER_ROUND        = 25     PHASED_WRITE_ENTRIES    = 30
GAP_DENSITY_RATIO          = 0.34   MIN_ENTRY_COVERAGE      = 0.6
MERGE_SIMILARITY           = 0.5    MAX_NEW_FOLLOWS_PER_RUN = 1
```

What to read after the first real follow, in `debug/<date>/dossier/<issue>/`:

- `index.json` — `rounds`/`calls` (did it saturate, or hit the ceiling?),
  `sides` (did the entity table stay one-sided?), `unreadable` (how much did
  `extract.py` fail to get?)
- `discarded-questions.json` — if the drift guards cut something that mattered,
  `MIN_QUESTION_SCORE` is too high
- the `write_rejected` events in `trace.jsonl` — an `entry_coverage` rejection
  means the ledger is good and the writing is not, which is a different problem
  from a thin ledger


