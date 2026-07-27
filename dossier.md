# Dossier — Deep Research for Followed Stories

Settled 2026-07-27, after the first two live follows. This document specifies a
replacement for Follow's single-call backstory. It is additive to
`product.md`, `research.md` and `decisions.md`, which remain the specification
for everything else; where it deviates from a settled decision, §14 says so
explicitly.

Read this with `CLAUDE.md`'s "Architecture" and "Reliability model" sections.
The invariants in §13 are the ones that must not be broken.

## 1. Why

`followed/2.json` — "PM Modi creates exam reform task force after minister
resigns over leaks", followed 2026-07-27 — is the motivating failure. The
owner had watched the story unfold through live journalism and knew its actual
arc: a hunger strike, the striker removed by police to hospital, a resulting
surge in protest, an aggressive police response (lathi charge, tear gas,
pellet guns), then de-escalation after the minister resigned.

The generated backstory contained none of it. What the debug capture
(`debug/2026-07-27/follow/`) shows:

| | |
| --- | --- |
| prompt sent to Gemini | **188 characters** — headline, date, section, one instruction |
| latency | 12.2 s |
| search queries the model ran | **4**, every one framed from the state's side |
| source chunks consulted | 19 (search *snippets*, not articles) |
| citation spans in the output | **8**, over 3,426 characters |
| output | 524 words, of which ~160 recite the task force members' CVs |

Five causes, and each one is addressed below:

1. **The pipeline already had the missing facts and discarded them.**
   `data/2026-07-27.json` carried ten claims for this story, including
   *"Nineteen-year-old student Sahil Lochab faces the potential loss of an eye
   after being hit by pellet guns during student demonstrations"* (Hindustan
   Times), *"Rahul Gandhi sent a letter to Home Minister Amit Shah demanding
   accountability for police actions against student protesters in Delhi on
   July 20"* (Hindustan Times), and *"Youth activist group Cockroach Janta
   Party called off its protests on Saturday after the government accepted its
   demands"* (Channel News Asia). `_new_follows()` resolves the story in
   `data/`, confirms it exists, and then builds its prompt from the headline
   and date alone. → §4 Pass A.
2. **Grounding returns citations, not content.** Google Search grounding
   supplies snippets; the `url_context` tool exists specifically to go beyond
   them. Snippets favour what is cleanly stateable in a headline — *a minister
   resigned*, *a task force was named* — while a multi-day escalation lives in
   article bodies. → §4 Pass D.
3. **The headline was the frame, and the frame was the ceiling.** All four
   queries orbited the day's news peg. The subject is not "PM Modi creates a
   task force"; it is the 2026 exam-leak protest movement. → §4 Pass B.
4. **500–700 words cannot hold the story.** → §11.
5. **One shot, no completeness check.** `ground.research()` rejects a response
   only when it has zero citations. Shallow-but-cited passes. → §7, §8.

A sixth, structural point: this story was followed *after* its climax, so the
timeline had nothing legitimate to add and the backstory was the entire
product. Follow must be good for stories caught late, not only for stories
caught early.

## 2. The central change

Research and writing become separate artifacts.

> The **dossier** is the source of truth for a followed story's evidence.
> Prose is derived from it, the way `docs/` is derived from `data/`.

Today a follow holds one block of prose and the sources Gemini happened to
cite. Under this spec a follow holds an accumulating, append-only evidence
base — dated events, entities, source texts, open questions — and the prose on
the page is a rendering of it.

This is the same posture the digest already takes: the write pass sees claims,
never raw article text, so a fact that is not an anchored claim has no way into
the prose. Follow gains the same discipline, with ledger entries in place of
claims.

## 3. The dossier

One dossier per followed story, at `followed/<issue>/dossier.json`. Written
only by `dossier.py`. Append-only: entries and entities are added and may be
corrected, never silently dropped.

```
issue            int
subject          str    the story's real name, as determined in Pass B —
                        not necessarily the headline that was followed
span             {start: date, end: date}   the story's own timeline
research_state   pending | researching | complete | capped
rounds           int
calls            int    grounded + extraction calls spent, lifetime
ledger           [Entry]
entities         [Entity]
questions        {open: [Question], asked: [str]}
corpus           {url: {outlet, fetched_at, chars, text}}
chips            [str]  every distinct searchEntryPoint.rendered_content
checkpoint       {stage, round, updated_at}
```

**Entry** — one dated event.

```
id           int          stable, monotonic; the token the write pass cites
date         YYYY-MM-DD   or YYYY-MM for an imprecise one
precision    day | month
what         str          one sentence, plain
actors       [str]        entity names
sources      [{outlet, url}]
outlet_count int          derived; drives attribution at write time (§11)
phase        str|null     assigned in §7
added_round  int
```

**Entity** — one named person, organisation or place.

```
name         str
kind         person | org | place
role         str|null     null means unexplained → raises a question (§6)
side         state | movement | other | unknown
first_seen   date
last_seen    date
```

**Question** — one unit of research work.

```
text         str
origin       model | entity | gap | dangling | contested
depth        int          branch depth; 0 for Pass B's plan
score        float        relevance, 0-1 (§8)
parent       int|null     question id it descends from
```

`corpus` holds extracted article text keyed by URL. It is the largest part of
the file; see §10 for the storage consequence.

## 4. The research passes

Pass A runs once. Passes B–G are one research *round*; rounds repeat per §7.

**Pass A — seed from what is already known. 0 API calls.**
Load the origin story from `data/<date>.json`: body, all claims with their
outlets and URLs, and the story's own source list. Every claim becomes a
provisional ledger entry, and every source URL is extracted into the corpus
with `extract.py` (keyless, already handles JSON-LD → paragraphs → Jina
escalation). On the motivating story this alone puts the pellet-gun injury,
the Rahul Gandhi letter, and the protests being called off into the dossier
before any search runs.

**Pass B — name the story and plan the research. 1 grounded call.**
Two outputs. First the `subject`: given the headline and Pass A's evidence,
what is this story at full scope? The model is explicitly permitted — and
expected — to rename it away from the day's peg. Second, the initial question
set, 25–40 questions, generated against a **mandatory dimension checklist**:

- origin and root causes
- key individuals **by name, on every side** — organisers, activists,
  officials, victims
- the movement thread — who organised, tactics, hunger strikes, marches, sit-ins
- state response — police action, force used, detentions, injuries, deaths,
  curbs on assembly or communication
- institutional and legal — courts, inquiries, agencies, arrests
- political — parties, resignations, statements, opposition
- human consequence — who was hurt, who lost what
- contested or disputed facts, and who disputes them

The checklist is not advice; a round is incomplete until every dimension has
at least one question spent on it. Four of these eight dimensions were never
touched by the failing run, and three of those four are where the owner's
missing facts live.

**Pass C — search, narrowly and in parallel. 1 grounded call per question
batch.**
One call per question, or per small group of closely related questions — never
one call covering the whole story. A narrow scope produces narrow queries,
which produce relevant snippets. Breadth comes from many narrow calls.
Each call returns findings plus source URLs.

**Pass D — read the sources. `url_context` and `extract.py`.**
This is the depth fix. Every URL surfaced by Passes A–C is pulled as **full
text** into the corpus:

- `url_context` — generally available, up to **20 URLs per request**, 34 MB per
  URL, combinable with `google_search` in the same request, and it accepts PDFs
  and images.
- `extract.py` for bulk fetching, because it costs no quota at all.

Order: try `extract.py` first (free), escalate to `url_context` for anything it
cannot get. Expect 40–80 articles for a large story; 60 articles at ~4,000
characters is ~240k characters, comfortable inside the model's context window.
Grounding alone can never reach this, which is why the failing run could not.

**Pass E — extend the ledger. 1 call, search tool OFF.**
Feed the round's new corpus text in and extract dated events, actors, and
supporting sources. No prose.

With the search tool off, `response_schema` works — the documented
`400 INVALID_ARGUMENT` applies to `response_mime_type="application/json"`
*alongside* `google_search`, not to schema use in general. So this pass gets
real validated JSON rather than a delimited text format.

New entries are merged into the ledger by (date, what) similarity; a duplicate
event found in a second outlet increments `outlet_count` and appends its
source rather than creating a second entry.

**Pass F — generate the next questions.** Per §6. Runs inside Pass E's call
where practical, to save a round trip.

**Pass G — completeness critic. 1 grounded call.**
Given the subject, the ledger, and the entity table: what significant events
in this story are missing? Any named person absent? Any injury, death, arrest
or turning point uncovered? Any phase underweighted? Its output is questions,
not prose, and they enter the frontier.

## 5. The frontier

Rounds are driven by a work queue, not a fixed script.

```
open      questions not yet researched, priority-ordered by score then origin
asked     normalised question texts already spent — never re-asked
```

Each round pops the top questions (up to `QUESTIONS_PER_ROUND`), researches
them via Passes C–E, and Passes F/G refill the queue. Normalisation for the
`asked` set is casefold + whitespace collapse + stopword strip, the same
cheap shape `follow._normalise` already uses for headlines.

## 6. Where questions come from

Only the first of these is the model free-associating. The rest are
mechanical, which is the point — they cannot be talked out of noticing.

1. **model** — "given what is now known, what should be dug into next?"
2. **entity** — an entity appears in a ledger entry but has `role: null`. *"A
   name appears in a July 12 event and nowhere else"* is a detectable hole.
3. **gap** — see §7.
4. **dangling** — corpus text refers to an event not in the ledger ("after the
   earlier crackdown", "since the incident"). Cheap to detect, high yield.
5. **contested** — two sources give different figures for the same event. This
   becomes a question, never an averaging decision; `_PROSE_RULES` already
   forbids blending numbers, and this makes *finding* the disagreement part of
   research rather than luck.

## 7. Termination, phases, and the gap detector

**Date density and phases.** Bucket the ledger by week across `span`. Two uses:

- Weeks that are sparse relative to the story's own median density become
  `origin: gap` questions — *"what happened in this story between 10 June and
  20 July?"* The failing run's implied ledger was May 3, May 12, June 6,
  **nothing for seven weeks**, then July 24–27. A seven-week hole in a rapidly
  escalating national story is not a quiet period, and detecting it takes
  arithmetic, not judgement. This single check forces the searches that surface
  the hunger strike, the removal to hospital, and the police response.
- Contiguous dense runs become `phase` labels, reused by the write pass (§11).

**Entity asymmetry.** Count entities by `side`. A protest story whose entity
table is *Modi, Pradhan, Joshi, NTA, Nilekani, Somanath, Deka, Karwal, Meena,
Kamakoti* — ten entities, `side: state`, **zero from the movement** — has
researched one half of its subject. Also arithmetic. Also would have fired on
the failing run. A story with protest-thread ledger entries and no
movement-side entities raises questions until it has some or the round budget
is spent.

**Saturation.** Research stops when a full round adds fewer than
`SATURATION_ENTRIES` new ledger entries, `SATURATION_ROUNDS` times
consecutively — loop-until-dry rather than loop-N-times, because a fixed count
either quits early on a large story or burns calls on a small one. The two
gap detectors above must also be clear.

**Ceilings.** `MAX_CALLS_PER_FOLLOW` is a hard backstop. When a ceiling stops
research, `research_state` becomes `capped`, and the cap is logged with what
was left in the frontier. A truncated dossier must never present as a complete
one.

## 8. Drift guards

Recursive research drifts: *"who is Nandan Nilekani"* → *"what is Infosys"* →
*"history of the Indian IT industry"*, and the result is four thousand
well-sourced words answering a question nobody asked. Three guards:

- **Branch depth cap** — `MAX_QUESTION_DEPTH`. A question descended from a
  question descended from a question is where drift begins.
- **Relevance gate** — every generated question is scored for "does answering
  this make *this* story clearer to someone following it?" before entering the
  frontier. Below `MIN_QUESTION_SCORE` it is discarded, and recorded as
  discarded. This is the primary defence.
- **Ledger scoping** — an entry must attach to `span`. Deep background is
  legitimate as context *for* an entry, never as a new branch of its own.

With no word limit and forty calls available, the failure mode inverts from
too shallow to too long and unfocused. These guards and §11's writing
discipline are what hold it.

## 9. Checkpointing and rate limits

**Every completed call is checkpointed.** `dossier.json` is written after each
call, with `checkpoint.{stage, round}`. This — not a larger
`ratelimit.WAIT_BUDGET_S` — is how a research job "completes no matter what",
because it converts every failure mode into a pause:

| Failure | Behaviour |
| --- | --- |
| minute-scoped 429 | `ratelimit.call_with_resume` waits and resumes, as today |
| wait budget exhausted | job checkpoints; every prior call is banked |
| **PerDay quota exhausted** | `ratelimit` re-raises immediately (correctly — it must not sleep for hours); job checkpoints; one of the three staggered `--if-missing` crons resumes it tomorrow |
| process killed, runner times out | next run resumes from `checkpoint` |
| extraction failure on one URL | that URL is skipped and recorded; the round continues |

A day-scoped 429 therefore stops being fatal and becomes "finishes tomorrow".
`research_state: researching` is what a resuming run looks for.

**A daily call budget.** Because §12 gives each active follow its own research,
recurring cost scales with the number of follows. `MAX_RESEARCH_CALLS_PER_DAY`
bounds total grounded calls across all follows per morning, spent
stalest-follow-first. A follow that does not fit waits for tomorrow and is
logged as deferred. This is the same "degrade, don't fail" posture as
`feeds.quorum_ok`.

**Follow is still never on the digest's critical path.** `digest.yml` keeps
`continue-on-error: true`. A dossier job that fails, stalls or gets capped
cannot stop the morning's digest from publishing.

## 10. Storage

```
followed/<issue>/
  record.json     the existing followed/<issue>.json contract, unchanged in shape
  dossier.json    ledger, entities, questions, chips, checkpoint
  corpus.json     {url: extracted text} — split out because it is large
```

`follow.load_all()` reads `followed/<issue>/record.json`, with a fallback to
the legacy flat `followed/<issue>.json` so existing follows keep rendering.
The migration is one-way and lazy: a legacy record is moved into its directory
the next time it is touched.

`corpus.json` is committed. It is the evidence, and this repo already commits
`debug/` on the same reasoning — a record that only exists in a expired
Actions log is not a record. If it becomes a size problem, corpus text may be
truncated per URL before the ledger is built, never after, and the truncation
must be recorded.

**Extraction is cached by URL across follows and across days.** Fourteen days
of updates on one story, or two follows on related stories, will otherwise
re-fetch the same background articles repeatedly. The cache is keyed by URL
with a `fetched_at`; a hit costs nothing.

## 11. Writing

**No word limit.** The target is derived from the ledger, not fixed: enough
prose to carry every entry the reader needs, in the story's own phases.
`MAX_OUTPUT_TOKENS` (currently 8192) is raised accordingly, for both the
ledger and the write passes.

**Written from the ledger, never from the corpus.** The writer sees entries,
exactly as the digest's write pass sees claims. A fact that is not a ledger
entry has no path into the prose.

**Anchoring changes mechanism, and this is an improvement.** Today Follow's
markers come from Gemini's `grounding_supports`, which is why 3,426 characters
carried only 8 citation spans — most of that backstory was unsourced prose.
Under this spec the write pass runs with the search tool off and cites ledger
entries with `[eN]` tokens, which `anchor.py` converts to markers using the
same machinery it already applies to the digest's `[cN]` claim tokens. The
existing floors — `MAX_UNANCHORED_SHARE`, `MIN_MARKERS`, `MIN_BODY_WORDS` —
then apply to followed-story prose for the first time. Depth and anchoring
improve together instead of trading off.

**Attribution follows `outlet_count`.** An entry carried by one outlet is
written with explicit attribution ("Hindustan Times reported"); an entry
corroborated by several may be stated flatly. `_PROSE_RULES` already requires
this; the ledger makes it an enforceable number rather than an aspiration, in
the spirit of `anchor.THIN_MIN_CLAIM_OUTLETS`.

**Phased writing for large ledgers.** Above `PHASED_WRITE_ENTRIES`, the prose
is written one phase at a time (phases from §7) and concatenated, rather than
in one call. Each call stays small and no phase gets squeezed.

**`_PROSE_RULES` is unchanged and still binding** — plain text only, no
markdown, no headings, no "Why this matters" or "What to watch" sections, one
continuous piece of prose, attribution kept on contested claims, numbers never
blended. Removing the word limit does not license a research dump.

**Prose is regenerable; research is not repeated.** `follow.py`'s docstring
currently says the backstory is never regenerated. That rule existed to
protect quota and `product.md`'s "grows the fuller picture" promise, and both
survive here: regeneration re-runs the *write* pass over an append-only ledger,
costing one call, never re-researching from scratch. The promise holds because
the ledger only ever grows. This is a docstring rule, not a `decisions.md`
rule, and it is superseded.

**Search Suggestion chips.** `decisions.md:68` requires displaying them, as the
grounding Terms require. With many grounded calls there are many
`searchEntryPoint` blobs; the dossier stores every distinct one in `chips`, and
the followed-story page renders the deduplicated set.

## 12. The daily timeline pass

The same loop, scaled down: research → new ledger entries → append → write
only what is new. One or two rounds per active follow, not ten.

Three consequences:

- **Dedupe becomes precise.** The update pass is given the ledger, replacing
  `_recap_lines()`'s "first sentence of the last six entries" heuristic.
  "Report only what is new" stops being a vague instruction.
- **"Quiet" becomes mechanical** — zero new ledger entries for the period —
  instead of a model judgement about whether today was quiet. More trustworthy
  in both directions, and a quiet day still appends nothing.
- **The dossier compounds.** After three weeks of following, the reader has a
  real accumulating account rather than twenty disconnected paragraphs, which
  is what `product.md:57-59` asks for.

`STALE_DAYS` closure (14 days without development) is unchanged, and a closing
update is still written as the story's final entry.

## 13. Invariants

- The dossier is **append-only**. Entries may be corrected or merged; they are
  never silently removed. Whatever a reader saw yesterday is still accounted
  for today.
- `followed/<issue>/` is a **source of truth** alongside `data/`. Every page in
  `docs/` remains derived and overwritten wholesale on render.
- **The write pass never sees corpus text** — only ledger entries. Numbers are
  never blended across sources; two disagreeing figures are two entries, each
  attributed.
- **Nothing follows itself.** Unchanged, and non-negotiable. A dossier exists
  only because the owner opened a labelled issue, and both the workflow guard
  and `follow.fetch_issues()`'s independent author check still apply.
- **Follow can never take the digest down.** Unchanged.
- **No silent caps.** Every ceiling that bites — call budget, daily budget,
  corpus truncation, discarded question — is recorded in the dossier and
  logged. A capped dossier reports itself as `capped`.
- **A follow being researched says so.** Until `research_state` is `complete`,
  the page renders an honest "researching this story" state rather than an
  empty or partial one — the same posture `render._stale_html` takes toward a
  stale digest.

## 14. The spec deviation, signed off

`decisions.md:70` reads: *"**No cap** on active follows; quota is protected by
batching."*

Batching is no longer the protection. A new follow costs a burst of research
calls, and §12 gives each active follow its own daily research instead of one
call batched across all of them. Recurring cost scales with the number of
follows: five active follows at two calls each is ten grounded calls a morning
on top of the digest's three.

The protection mechanism becomes, in order:

1. `MAX_CALLS_PER_FOLLOW` — the one-time research burst is bounded
2. `MAX_RESEARCH_CALLS_PER_DAY` — recurring cost across all follows is bounded,
   spent stalest-first, with deferrals logged
3. saturation exit (§7) — most stories stop well below the ceilings
4. checkpointed resumption (§9) — exceeding a budget delays, never fails
5. `MAX_NEW_FOLLOWS_PER_RUN` drops from 3 to 1, so a burst of new follow
   requests cannot stack research bursts in one morning

The owner signed off on this deviation on 2026-07-27. Record the change of
approach in `calibration.md`; `decisions.md` itself stays settled and unedited
per `CLAUDE.md`.

## 15. Dials

Tune these, backed by an observation logged in `calibration.md`, before
rewording a prompt.

| Dial | What it does |
| --- | --- |
| `QUESTIONS_PER_ROUND` | how many frontier questions a round pops |
| `SATURATION_ENTRIES`, `SATURATION_ROUNDS` | the loop-until-dry threshold |
| `MAX_CALLS_PER_FOLLOW` | hard ceiling on one follow's lifetime research |
| `MAX_RESEARCH_CALLS_PER_DAY` | ceiling on recurring cost across all follows |
| `MAX_QUESTION_DEPTH` | branch depth before a line of enquiry is cut |
| `MIN_QUESTION_SCORE` | relevance floor for entering the frontier |
| `MAX_URLS_PER_CONTEXT_CALL` | `url_context` batch size (API maximum 20) |
| `PHASED_WRITE_ENTRIES` | ledger size above which prose is written per phase |
| `GAP_DENSITY_RATIO` | how sparse a week must be to raise a gap question |
| `MAX_NEW_FOLLOWS_PER_RUN` | 3 → 1, per §14 |
| `MAX_OUTPUT_TOKENS` | raised from 8192 for the ledger and write passes |

Starting values are deliberately unset here; they are the first thing to
calibrate against real runs, and `DIGEST_DEBUG` capture is what they should be
calibrated from.

## 16. Modules

- **`dossier.py`** (new) — the dossier contract, the frontier, the gap
  detectors, the question generators, saturation and checkpointing. Owns
  `followed/<issue>/dossier.json` and `corpus.json`, and nothing else writes
  them.
- **`ground.py`** — gains `url_context`, and a schema-capable call path for
  search-off passes. Its existing byte-offset and redirect-resolution
  behaviour is unchanged and still load-bearing.
- **`extract.py`** — gains the URL-keyed cache. Otherwise unchanged.
- **`anchor.py`** — its `[cN]` marker validation is reused for the write pass's
  `[eN]` tokens.
- **`follow.py`** — keeps issue parsing, the owner guard, resolution, closure
  and orchestration. It stops generating prose directly and calls `dossier.py`.
- **`render.py`** — gains the in-progress research state; still renders prose
  and sources through the existing `_grounded_html` shape.
- **`ratelimit.py`** — unchanged. It already does the right thing; §9 relies on
  that.

Naming: "dossier" rather than "research" because `research.md`, `report.py` and
`ground.research()` already exist, and a fourth thing called research would be
unreadable a year from now.

## 17. Explicitly out of scope

Considered on 2026-07-27 and deliberately not included. Recorded so a future
reader knows they were weighed rather than missed.

- **Deliberate opposing-frame research** — per-side searches for the state's
  account versus the movement's account versus independent and fact-checking
  coverage. The failing backstory reproduced the resigning minister's
  "anti-national forces" framing at length with no counterweight. §4 Pass B's
  checklist partly covers this through its state-response and human-consequence
  dimensions, but sourcing *symmetry* is a separate axis and is not specified
  here.
- **Liveblog and local-language sources** — `/live/` URLs, video transcripts,
  and Hindi-language reporting. A liveblog is a timestamped event ledger
  written by a human on the scene, which makes it the richest possible input
  for exactly the granularity this failure was about. Not specified; the
  highest-value future extension.

## 18. Acceptance

This feature is working when, re-run against the 2026-07-27 exam-leak story,
the dossier contains dated entries for the hunger strike, the removal to
hospital, the subsequent escalation, the police response including the
pellet-gun injury, and the de-escalation after the resignation — each anchored
to a source — and the prose carries all of them.

That is the specific test. `followed/2.json` and
`debug/2026-07-27/follow/ground-1-backstory-2.*` are kept as the before case.
