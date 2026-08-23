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

## 2026-08-01 — Follow froze, and the proxy started 403ing us

Five committed days of production data (`ANALYSIS-2026-07-31.md`, read against
`data/`, `debug/`, `followed/` at `2b2e02f`). The digest itself is healthy —
`health` exits 0, 14/14 feeds, exactly 3 LLM calls a morning, zero stories
dropped at the anchor gate. Everything below was invisible to the project's own
instruments; each is fixed here with a regression test that fails without it.

**Follow #3 was frozen, and said "complete".** `followed/3/dossier.json`:
`research_state: complete`, `rounds: 10`, `calls: 54`, **631 open questions**,
frozen across three CI commits while `docs/follow-3.html` rendered "The full
picture". It was out of lifetime budget — `54 + write_reserve(154) = 61 >= 60`,
so `research()` took the `call_ceiling` branch, wrote `capped` to disk, and
returned it. `_update_follow` discarded the return value and wrote `complete`
over it in the same call stack. All three of 2026-07-31's runs are identical in
`trace.jsonl`: `dossier: #3 CAPPED at 54 call(s)` immediately followed by
`follow: #3 -> quiet`, with the grounded pool sitting untouched at 18/18. The
tracer was right; the state that reached the page was not.

**Why 54 calls bought so little: the gap detector had no upper bound.** #3's
span is 2015-05-01 → 2026-07-28 — 587 weekly buckets, entries in 40. With a
median of 0 the density floor falls back to 0.34, so *every empty week clears
it*: `gap_questions()` returned **547 questions**, and 616 of the 631 in the
frontier were "what happened in this empty week of 2017". They score 0.75,
second only to the daily delta, so `pop_round` fed them to Pass C ahead of
almost everything. Rounds 6-10 spent 22 calls for 97 entries with the frontier
pinned above 600; at ~9 drained per round, emptying it needed ~70 more rounds
against a ceiling 6 calls away. And because `saturated()` requires the detector
to be *clean*, the loop-until-dry exit was dead code for any long-span story —
research could only ever end on a ceiling.

**`STALE_DAYS` was inverted.** The `_is_closing` check sat after the quiet
path's early return, so it was reachable only when research had *found*
something. A story that went silent returned early every day and never closed;
a story that came back after a fortnight closed on the news that proved it
hadn't, labelled `kind: "final"`. Combined with the above, #3 could neither
update nor close.

**`r.jina.ai` now 403s a browser User-Agent.** 2026-07-31: 13 escalations, 13
returning 0 characters; 9 articles ended with no body at all (4 NDTV, 5 France
24), 4 more under the 600-char floor — 13 of 45 articles, 29% of the day.
2026-07-30 the same shape; 07-27/28/29 fine, so it started around 07-30.
Isolated by probing one NDTV URL three ways, then re-verified live today:

| UA sent to `r.jina.ai` | result |
| --- | --- |
| `feeds.UA` (Chrome 126) | **403** |
| none / curl's own / `python-requests/2.32.3` / `JINA_UA` | **200**, 22.8 KB |

The digest degraded honestly — those articles were labelled `(SUMMARY ONLY)`
and counted in `source_kinds`, and no claim was invented — but claims then rest
on a 400-char RSS blurb. Summary-sourced claims went 0, 0, 3, **7** across
07-28→07-31, and 07-31's India lead published on 2 outlets because the four
NDTV articles on exactly that story all extracted 0 chars.

### Dials turned

```
GAP_CONTEXT_WEEKS   (new) = 8    a sparse week is only a hole if the story is
                                 active within 8 weeks on BOTH sides, so nothing
                                 is raised for a hole wider than 15 weeks
MAX_GAP_QUESTIONS   (new) = 12   per detector run, most recent first, and the
                                 held-back count is logged
GAP_DENSITY_RATIO    0.34 -> 0.34   unchanged; the ratio was never the problem
```

Measured against the committed dossier #3: **547 gap questions → 44 bracketed →
12 admitted per run**, with `gap detector held back 32 of 44 question(s)` in the
log. dossier.md §7's motivating case (a seven-week silence mid-story) still
raises all eight of its weeks, and the existing tests for it pass unchanged.
`gap_questions()` also now takes the frontier's `asked` keys, which is what
makes `saturated()` mean "every hole we can see has been asked about" rather
than "every hole has been filled" — a week that stayed empty after we searched
it is answered, not outstanding.

### Code fixed, not dialled

- `_update_follow` honours `research()`'s verdict; a `capped` dossier stays
  capped and short-circuits the next day's update instead of admitting a delta
  question it can never pop.
- `_close_if_stale()` runs on the quiet paths and never after a development.
  Staleness is measured from `max(last_development, started_at)` so a story
  followed off an archive page is not born stale. No timeline entry is appended
  on a quiet close — the last real update is relabelled `final`.
- `extract.JINA_UA` for the proxy; `UA` still for publisher pages.

`watch next:` (1) `followed/3/dossier.json` should flip to `capped` on the next
run and the page should show "Research paused" — it self-heals in one run, no
migration needed; #3 then closes by `STALE_DAYS` from its last real development.
(2) `jina_fired` vs `jina yielded>0 chars` in `debug/<date>/extract/index.json`
— expect them equal again, and `source_kinds.summary` back to 0. (3) Whether
`saturated()` ever actually fires now on a long-span follow, or whether
`MAX_ROUNDS`/the lifetime ceiling still gets there first (F5 in the analysis:
`MAX_ROUNDS` exhaustion still reports `complete` with no `dbg()` line, which is
the next silent cap to close).

## 2026-08-23 — the health job had been red for 23 days, and was right

The owner's actual symptom: a failure email from nearly every scheduled run,
three times a day, for three weeks. The digest was never the thing failing.
`build` and `deploy` were green in every run sampled; the red was always the
third job, `health` — the feed-decay alarm doing precisely what it was built
to do, into a room that had stopped listening.

**Indian Express has 403'd from CI since 2026-08-01.** Read off `data/*.json`,
which carries `health.feeds` per day and so is the whole record:

| window | http | usable |
| --- | --- | --- |
| 2026-07-27 → 07-31 | 200 | 200 items/run |
| 2026-08-01 → 08-23 | **403** | **0**, 23 consecutive days, never once a 200 |

`DEAD_DAYS` is 3, so `feed_health` cleared `ok` on every run from 08-04 on.

**It is not the client, and there is nothing to fix in the code.** Probed
2026-08-23 from a home IP in India, `https://indianexpress.com/section/india/feed/`:

| how | result |
| --- | --- |
| `requests.get(url, headers={"User-Agent": feeds.UA})` — the exact `feeds.py` path | **200**, 212 KB |
| bare `curl`, no UA | **200** |
| `curl -A` a current Chrome UA | **200** |
| the same request from a GitHub Actions runner | **403**, every run for 23 days |

The response is CloudFront-fronted (`x-amz-cf-pop: BOM78`, Mumbai). The
identical request succeeding from a residential IP and failing from CI puts
the block at the CDN/WAF layer against Actions' datacenter range — the same
week `r.jina.ai` started 403ing us for a neighbouring reason (2026-08-01
above, which fixed the proxy and never noticed the feed). Unlike the proxy,
there is no second UA to try: the UA is already innocent.

**So the feed is retired, not debugged.** `feeds.txt` drops it to the excluded
block with the measurement and the date, alongside the 2026-07-25 exclusions.
India keeps six live feeds (The Hindu, NDTV, Hindustan Times, Times of India,
Livemint, Scroll.in) and the digest has been publishing at 13/14 throughout,
far above `MIN_LIVE_FEEDS` — no morning was ever thinner for this.

### The bug retiring it exposed

Removing the line from `feeds.txt` would **not** have cleared the alarm.
`feed_health` iterates `seen_today | outlets.keys()`, so an outlet is carried
forward once seen anywhere in the window — deliberately, because that is what
catches a feed that stops being fetched at all instead of failing with a
status code. But it also means a retired feed keeps accruing dead days until
it ages out of `HISTORY_DAYS`: the job would have stayed red for **seven more
days** after the fix, which is exactly the failure mode that produced this
entry in the first place.

`feed_health` now takes the configured roster and drops outlets that are no
longer in it. Retirement is same-day; a *configured* outlet missing from a
day's health is still dead, so the silent-decay catch is untouched. The
roster is read from `feeds.txt` in `_cmd_health`, not from the data files —
reading it from the data would reintroduce the bug. Three tests in
`tests/test_report.py` hold all three halves of that line, including the
carry-forward one, which is the property most likely to be optimised away by
someone who reads only the retirement case.

### Not fixed, recorded

- **`data/2026-08-07.json` is the only missing day in the record.** All three
  fires lost it to a Gemini 503 on `select`
  (`debug/2026-08-07/run.json`: `stages[-1].error`, "high demand ... try again
  later"). Two more `build` failures in the last ten runs (08-13, 08-18) died
  the same way and self-healed — a later staggered fire re-ran and
  `--if-missing` no-opped the rest. The stagger is doing its job; 08-07 is
  what it looks like when all three draws come up bad. No dial moved: one lost
  day in 28 is the free tier working as designed, and a retry loop around
  `select` would spend the schema pool to buy it back.
- **France 24 at 6/7**, one silent day, below `DEAD_DAYS` and correctly not an
  alarm. Noted only so the next reader of the table knows it was seen.

`watch next:` whether the health job goes green and *stays* green — a red
health job is now meaningful again, and the first one that fires is a real
feed dying, not this. If a second publisher starts 403ing only from CI, the
pattern is no longer a coincidence and the gather path may need the
`r.jina.ai` escalation `extract.py` already has, which would be the first
network dependency in `feeds.py` and should not be added for one outlet.

## 2026-08-23 (later) — the full-record analysis, and what it changed

`ANALYSIS-2026-08-23.md` reads the whole live record: 27 published days, 216
stories, 1,823 claims, 41 captured digest runs. The trust machinery held —
zero structural violations across all 216 stories, and `docs/` re-renders
byte-for-byte from `data/` + `followed/`. What the 5-day window could not show
was three real defects and one operational cliff. This entry logs what moved.

### Corrections to the entry above

Two numbers in "Not fixed, recorded" were wrong, and they were the evidence
behind deciding not to retry:

- **The 503 killed 15 of 41 captured runs, not 2.** Counted from
  `debug/*/trace.jsonl` across all 28 captured days: 7 aborted at `claims`, 4
  at `select`, 3 at `write`, 1 on a `RemoteProtocolError` at `claims`.
- **2026-08-18's digest run was clean** — one run, every stage `ok: true`, 181 s
  end to end (`debug/2026-08-18/trace.jsonl`). 08-13 did die on a 503; 08-18 did
  not.
- And the cost accounting ran the other way round from what that entry assumed.
  A run that dies at `claims` or `write` has already paid for `select` (and
  `claims`), and the later cron that rescues the day pays again: **13 successful
  LLM calls beyond the 3-per-morning invariant over 27 days**, every one a redo.
  Three days spent 5 calls, which also broke the schema pool's arithmetic — it
  was sized against a worst case of 4.

### changed

| dial | old -> new | why |
| --- | --- | --- |
| `ratelimit.TRANSIENT_ATTEMPTS` / `TRANSIENT_BASE_SLEEP_S` | — -> 3 / 5s | a 5xx or dropped connection now gets two short retries, on its own budget, separate from the 429 wait. 15 runs and one whole lost day, above |
| `llm.extract_claims` call structure | 1 call -> 1 per section | position decay, below. 4 LLM calls a morning now, not 3 |
| `dossier.MAX_SCHEMA_CALLS_PER_DAY` | 14 -> 12 | 20 measured − 6 for the digest at its worst (4 nominal + 2 for an aborted fire's paid stages) − 2 margin. The old 14 assumed a worst case of 4 that three days had already exceeded |
| `anchor.GATE_UNSOURCED_FIGURES` | — -> True | `unsourced_figures` was a diagnostic "too noisy to gate on". Over 216 stories it fired 9 times, 6 of them from three *mechanical* causes (a claim spelling the number out, an outlet name carrying a digit — France 24 — and a claim's `FY26` written out as a financial year). With all three closed it fires 3 times and every one is real: 3/3 precision, so it now drops the story |
| `report.RETAIN_DAYS` | — -> 7 | `debug/` was 472 MB over 28 days, 89% of the objects in a 510 MB depth-1 clone, which all three daily crons each pay for. Nothing bounded the directory; `MAX_RUN_BYTES` bounds only one run and has never once bitten |

### The claims pass decays with prompt position

The measurement, per-cluster counts from all 27 `debug/*/claims.json`, 212
clusters:

```
position 0: 12.96 claims (n=26)   position 5:  6.95 (n=22)
position 1: 10.27                 position 7:  6.59 (n=17)
position 2:  8.54                 position 9:  5.43 (n=7)
position 4:  7.62                 position 11: 4.00 (n=1)
```

Monotone, 3.2x first-to-last, and **85 of 212 clusters (40%) land below the 8
the prompt itself asks for.** It is not the outlet-count confound: paired within
one day, same section, identical distinct-outlet count, n = 124 pairs, the
earlier cluster won 70 to 21 (mean −1.14 claims, one-sided sign test
p = 1.3e-07). The write pass is innocent — words-per-claim is flat at 20.6–26.4
across every position.

**India paid for it structurally.** Every day's prompt ran World then India, 27
of 27 days, so India was always at the tail: 6.92 claims a cluster against
World's 9.33, on identical outlet counts. Both of the record's only two
anchor-gate drops were Indian stories at positions 9 and 10 on 2026-08-14, each
returning **3 claims from ~20 KB of cleanly extracted text**, `finish_reason:
STOP`, 4,813 of 32,768 output tokens used — no truncation, just less attention
at the tail.

One call per section resets position for India at the cost of one call a
morning. Nothing in the prompt is reworded, so this is a dial-shaped change and
not an editorial one; `claims.json` now records each call and each cluster's
`batch_position` so the same measurement can be re-run from the record.

### Not changed, deliberately

- **Word targets.** `lead` runs at 56% of `WORD_TARGET`, `major` 87%, `notable`
  73% — and body length tracks *claims available*, not the target (a `lead` with
  15+ claims reaches 366 words; the same instruction with 5–9 claims produces
  218). The dial is not being ignored, it is unreachable from the claim supply.
  Re-measure after the claims split before touching it.
- **`TIER_WEIGHT["notable"]`.** 21 kept against 112 cut over 27 days, and
  `floor_relaxed` has never fired. But `WIKI_BONUS = 2` — which the 5-day
  analysis said "cannot promote anything on its own" — was **decisive 15 times**,
  publishing 13 notable and 2 major stories that would otherwise have been cut.
  The cross-check does overrule volume bias; what it cannot rescue is a
  1-or-2-outlet story (2 + 0 + 2 = 4 < 5). Leaving the floor alone.
- **The Sudan gap.** 276 curated Wikipedia events scored over 27 days, 186
  uncovered (67%). 21 of them are the Sudanese civil war, against **0** published
  Sudan stories — and it is not a feed gap and not the floor: Sudan appeared in
  19 pool articles across 14 days, three distinct outlets carried it on both
  08-05 and 08-08 (enough for exactly the floor), and on 08-08 the curated event
  was line 5 of the select prompt with a matching NPR article in the same pool.
  Select still never proposed it. Acting on that is a **select-prompt** change —
  editorial judgement — so per `decisions.md` it goes to the owner first and is
  not in this commit.

`watch next:` whether India's claim counts converge on World's now that both
start at position 0, and whether any story is dropped by the new figure gate. A
drop there is not a regression — it is the gate doing its job — but a *second*
one in a week would mean the write pass has found a new route to a number the
claims never carried.

## 2026-08-23 (later still) — the reader said the prose is hard to read

Owner feedback, as the reader the app is for: "when reading, the language is
really hard to understand — I want easier language, that I can just read it
once and get it."

`product.md` §Reader already asks for "plain, easy language, not newspaper
English" and `decisions.md` §Editorial for "plain adult English". The output
was not meeting them. Rule 8 of `llm._WRITE_SYSTEM` asked for exactly that in
one abstract sentence — "short sentences, active voice, concrete nouns, no
jargon, no idioms" — and the model read it as a register hint and wrote wire
copy anyway. Measured over the last 7 published days, 62 stories, 625
sentences:

```
"stated" / "noted" / "asserted"       56    vs   "said" / "told"   11
nominalisations (-tion/-ment/-ance)  386          6.2 per story
"amid" / "following" / "prior to"     44          0.7 per story
"however" / "moreover" / "meanwhile"  36          0.6 per story
capitalised names per sentence                    4.1
sentences carrying a figure                        43%
mean words per sentence  18.8    p90  27    max  37
first sentence           21.4 words mean, 23 median
```

Two of those are the whole complaint. **Sources speak in "stated" five times
for every "said"** — and the claims pass hands the write pass claims already
phrased that way, so the register is inherited, not chosen. And **the first
sentence names a category, not an event**: "Trade relations between the United
States and Canada have worsened after bilateral negotiations broke down late
on Friday" (`data/2026-08-23.json`, lead). A reader who reads only that
sentence — which rule 7 says is the point of it — has learned that something
happened in trade. The 4.1 names per sentence come from repeating a full title
on every mention: "US Trade Representative Jamieson Greer" three times in one
story.

### changed

| what | old -> new | why |
| --- | --- | --- |
| `llm._WRITE_SYSTEM` rule 7 | "the first sentence carries the gist" -> WHO did WHAT, under 20 words, concrete subject, with two worked counter-examples | 21.4-word abstract openers, above |
| `llm._WRITE_SYSTEM` rule 8 | one abstract sentence -> seven named bans with the replacement word beside each | the abstract version measurably lost to the model's wire-copy prior |
| `llm._WRITE_SYSTEM` rule 9 | — -> one new fact per sentence; a figure sentence carries nothing else new | 43% of sentences carry a number, and they stack |
| `llm._WRITE_SYSTEM` rule 10 (was 9) | LENGTH unchanged, one clause added | "reach the length with more of the story, never by restating" — the 500-word lead was padding by repetition |

This is a register change, so `decisions.md` §Editorial requires showing it to
the owner before it ships; it was.

### Not changed

- **`anchor.WORD_TARGET`** stays 500/200/200. The lead pads, but the fix tried
  first is the rule-10 clause, not a shorter target — shortening it would lose
  story, and the analysis for it does not exist yet. Re-measure once the new
  prompt has a week of output.
- **The claims prompt** still says "stated", and that is correct: claims are
  internal and should stay close to the source's own wording. Rule 8 makes the
  write pass translate, which is the pass that faces the reader.
- **No anchoring rule moved.** Every gate in `anchor.py` is untouched, so the
  register change cannot buy readability with sourcing.

### What to check in a week

Re-run the counts above over `data/` for the 7 days after this ships. The two
that decide whether it worked: `said` should now outnumber `stated`, and mean
first-sentence length should fall under 20 words. If mean words per sentence
drops but the "stated" ratio does not, the ban list is being read as advice
rather than as a rule, and the next move is to put the bans in the user prompt
per story rather than the system prompt.

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


