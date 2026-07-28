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
2. the two per-model daily pools (`MAX_GROUNDED_CALLS_PER_DAY`,
   `MAX_SCHEMA_CALLS_PER_DAY`) — recurring cost across all follows, spent
   stalest-first, with every deferral recorded in
   `followed/_budget/<date>.json`
3. the saturation exit (§7) — most stories stop well below the ceilings
4. checkpointed resumption (§9) — exceeding a budget delays, never fails
5. `MAX_NEW_FOLLOWS_PER_RUN` dropped from 3 to 1

Owner signed this off on 2026-07-27. `decisions.md` itself stays settled and
unedited, per `CLAUDE.md`.

**Consequence to watch.** One new follow can still consume a whole day's pool
and defer every other follow's update. `_sweep`
orders unfinished research ahead of daily updates deliberately — a page stuck
saying "researching this story" is worse for a reader than a delayed one-line
update — but that is a choice, not something the dial values settle. If a
morning ever defers an update that mattered, the fix is to lower
`MAX_CALLS_PER_FOLLOW` below the daily cap, not to reorder the sweep.

## 2026-07-27 — first live dossier, and the real quota

Issue #3 (exam-leak story). Run green, 15m46s, **19 grounded calls**, all on
`gemini-2.5-flash`, then a PerDay 429 on the 20th. Banked 57 ledger entries,
77 entities, 52 pages, 28 outlets, span 2024-06-22 → 2026-07-27. Subject
renamed off the news peg to "India's Widespread Exam Paper Leak Scandal and
Student Protests". Every §18 acceptance fact present and sourced — hunger
strike, removal to hospital, pellet guns, tear gas and batons, Sahil Lochab's
eye and his surgery, the resignation — with Amnesty, HRW, Article-14,
Newslaundry and The Wire among the outlets, none of which the one-call
backstory had reached.

**What bit is still not certain, and that is now the system's problem, not
ours.** A grounded call is metered twice — the model's own requests-per-day
and the Google Search grounding allowance (reported as ~1.5K/day) — and they
are orders of magnitude apart. The 429 came after ~23 requests on
`gemini-2.5-flash` with `PerDay` in the quota id, which points at the model
RPD rather than grounding, but `quota_facts` was broken at the time so the
server's own number was never captured. The shipped dials said 40/day either
way, and everything ran on one model while a second pool sat idle.

`changed:`

- `ratelimit.quota_facts` fixed. Its regexes required JSON double quotes, but
  `google.genai` puts the payload on `exc.details` as a Python dict and
  `_blob()` repr()s it into single quotes — so it had matched nothing, ever,
  and the one 429 this project has recorded logged `facts: ''`. It now reads
  `GenerateRequestsPerDayPerProjectPerModel-FreeTier=20`. **The next 429 will
  state the real ceiling rather than leaving us to infer it.**
- Tool-less passes (Pass E, the writes) moved to `ground.SCHEMA_MODEL`
  (`gemini-3.6-flash`), so Follow draws on both pools instead of one.
- `Budget` meters the two pools separately; one being empty no longer stops
  work that would draw on the other.
- `QUESTIONS_PER_CALL` 3 → 5 and `CRITIC_EVERY` = 2, cutting a round from ~7
  grounded calls to ~3.
- The daily ceiling is now **learned rather than guessed**:
  `ratelimit.daily_limit()` reads it off the 429, `Budget.learn()` persists it
  to `followed/_budget/limits.json`, and it expires after
  `LEARNED_LIMIT_TTL_DAYS` = 14 so a raised limit is not ignored forever.
  Defaults are optimistic on purpose — one wasted call to discover the ceiling
  beats wasting most of the allowance daily. `MAX_GROUNDED_CALLS_PER_DAY` =
  120 (explore), `MAX_SCHEMA_CALLS_PER_DAY` = 14 (conservative: shared with
  the digest), `MAX_ROUNDS` 6 → 8, `MAX_CALLS_PER_FOLLOW` 40 → 60.

Net: **3 rounds/day → 6+**, with the ledger and write passes no longer
competing with searching for the same allowance. If grounding really does
allow ~1.5K/day, the binding constraint becomes the schema pool at ~14, which
is still four times what the first run managed.

`watch next:` the first `quota_learned` event in `trace.jsonl`, and
`followed/_budget/limits.json` — that is where the real ceiling finally gets
written down. Also whether the reset is really midnight Pacific. The 429 landed at
10:57 PT and calls worked again by 13:44 PT the same day — under three hours,
which does not match a midnight-Pacific RPD reset. Either the window is
rolling or the limit differs per model. The `quota_facts` fix is what will
settle it.

## 2026-07-28 — the ceiling, read off the dashboard instead of guessed

The previous entry said `watch next:` the first `quota_learned` event and
`followed/_budget/limits.json`. Neither ever appeared. The reason is in
`debug/2026-07-27/trace.jsonl`:

```json
{"stage": "ratelimit", "label": "dossier-3-r3-search",
 "verdict": "daily_quota_exhausted", "attempt": 1, "facts": ""}
```

`facts: ""` — that 429 carried no `quotaId` and no `quotaValue`, so
`ratelimit.daily_limit()` returned `None`, `Budget.learn()` never fired, and
`limits.json` was never written. **The learning mechanism was the only thing we
were relying on to find the ceiling, and it silently learned nothing.**

The number was sitting in AI Studio the whole time
(`aistudio.google.com/rate-limit`, read 2026-07-28, this key):

| | RPM | RPD | TPM |
| --- | --- | --- | --- |
| `gemini-2.5-flash` | 5 | **20** | ~250K |
| `gemini-3.6-flash` | 5 | **20** | ~250K |

**RPD 20 per model is the only binding meter.** Peak input tokens observed ~60K
against ~250K. Search grounding's allowance is 1,500/day, which 20 model
requests a day cannot reach in seventy-five days — so the previous entry's
"if grounding really does allow ~1.5K/day" was aimed at a meter that can never
fire first, and `MAX_GROUNDED_CALLS_PER_DAY = 120` was sized against it.

The 07-27 run confirms 20 exactly: 19 requests to `gemini-2.5-flash`, 429 on
the 20th. It also answers the open question from that entry — the peak-RPD
charts show ~31 attempts on 2.5 and ~27 on 3.6 against a limit of 20, i.e. both
pools were already being overdriven, and the "worked again under three hours"
observation was the *other* pool, not a rolling window.

**Second finding, from `tools/probe_ground_model.py` (new):
`gemini-3.6-flash` cannot use `google_search`.** Immediate 429, no quota
detail, twice — while a `url_context` call on the same model in the same run
succeeded, and `gemini-2.5-flash` grounded fine minutes later (control). Not a
dead pool, not an outage: search grounding is unavailable on Gemini 3 here, the
same way 2.5 Pro is `limit: 0`. `research.md` §3.1's "5,000 prompts/month on
Gemini 3 models" does not hold on this tier. `url_context` *does* work on 3.6
and returns the same `grounding_chunks`/`grounding_supports` shape `ground.py`
parses, so Pass D is movable and Pass C is not.

**Third finding: a round costs far more than the dials claimed.**
`batch_questions` groups by checklist dimension *before* splitting at
`QUESTIONS_PER_CALL`, so questions from distinct dimensions never share a call
and the worst case is one call per question. Round 1 on 07-27 spent **eight**
search calls, round 2 five — against the two that `ceil(10/5)` predicts. Two
rounds consumed 16 grounded calls and the entire day's pool.
`test_a_round_fits_inside_one_days_grounded_pool` encoded the same wrong
division and passed anyway (`120 // 4 = 30`), which is what let it ship.

### Dials turned

```
MAX_GROUNDED_CALLS_PER_DAY  120 -> 18   (20 measured - QUOTA_SAFETY_MARGIN)
MAX_SCHEMA_CALLS_PER_DAY     14 -> 14   (unchanged; 20 - digest's 4 - margin
                                         is what the arithmetic gives, so the
                                         value was right and the reasoning
                                         "conservative until measured" wasn't)
QUESTIONS_PER_ROUND          10 -> 6    (worst case 6 + 1 read + 1 critic = 8,
                                         which fits twice into 18)
QUESTIONS_PER_CALL            5 -> 3    (keeps a batch one topic asked three
                                         ways, now that the count of batches is
                                         understood to be the real cost)
MAX_ROUNDS                    8 -> 8    (8 x ~8 = ~64, which is what
MAX_CALLS_PER_FOLLOW         60 -> 60    MAX_CALLS_PER_FOLLOW already covered)
```

Net: **a follow spans three to four days at 20 RPD.** That is slow, and it is
what the quota physically allows — checkpointing is what makes it a pause
rather than a failure. The levers that would compress it are more pools, not
bigger numbers: moving Pass D to the schema model (verified possible, saves one
grounded call a round), or additional keys. Search capacity specifically cannot
be grown by adding models, because Pass C is pinned to the 2.5 family.

**Fourth finding, now fixed: a third 429 shape that `ratelimit.py` misread.**
The 3.6 grounding 429 carries no `quotaId`, no `quotaValue`, no `retryDelay`
and no "per day" text, so it classified as a waitable per-minute limit:

```
is_rate_limited True   is_daily_quota False   retry_after None
daily_limit     None   quota_facts    ''      WAIT_BUDGET_S 2700.0
```

`call_with_resume` therefore slept against the full 2700s budget — which is also
all of `dossier.MAX_RESEARCH_SECONDS` — retrying a call that could never
succeed. One such 429 consumed an entire research window.

`ratelimit.is_opaque_quota()` now recognises it: no quota named *and* no
retryDelay means the server is saying "you cannot do this", not "not right now".
Those get `OPAQUE_WAIT_BUDGET_S` (90s, derived from `WAIT_BUDGET_S` so
`DIGEST_WAIT_BUDGET_S=5` still shortens it) instead of the full budget —
measured 62s over 3 attempts, against 2700s before. Not grouped with daily
quotas on purpose: a single unexplained 429 on the digest's own write pass must
not turn a blip into a stale morning, so it still gets two real hops.

Three 429 shapes are now on record and each is handled distinctly: named
per-minute (wait out `retryDelay`), day-scoped (re-raise, let a later cron
resume), and opaque (two hops, then give up).

`watch next:` whether any *genuine* transient 429 ever arrives opaque — if
`opaque_quota_exhausted` shows up in `trace.jsonl` for a call that later
succeeds unchanged, 90s is too tight. Also whether Google ever starts attaching
violation details to the Gemini 3 grounding refusal, which would make it a clean
`limit: 0` and let `is_opaque_quota` retire.

## Starting dial values (2026-07-27, uncalibrated)

`dossier.md` §15 deliberately left these unset; they are the first thing to
tune against real runs, from `DIGEST_DEBUG` capture.

```
QUESTIONS_PER_ROUND        = 10     QUESTIONS_PER_CALL      = 5
MAX_ROUNDS                 = 8      SATURATION_ENTRIES      = 3
SATURATION_ROUNDS          = 2      MAX_CALLS_PER_FOLLOW    = 60
MAX_GROUNDED_CALLS_PER_DAY = 120*   MAX_SCHEMA_CALLS_PER_DAY = 14
     (* optimistic; the real ceiling is learned from the first 429)
CRITIC_EVERY               = 2      MAX_QUESTION_DEPTH      = 3
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


