# Meta Plan: Follow — a personal daily news digest

## Context

`SameerJadav/follow-news` is a greenfield project, built from scratch. The repo
currently holds three markdown documents and no code.

Those three documents are the specification and are already settled:
`product.md` (the product idea), `research.md` (free-tier findings, every
external fact verified against live endpoints on 2026-07-25), `decisions.md`
(19 settled decisions). **Read all three before planning any phase.** They are
not background reading — they are the contract, and the phases below assume you
have read them.

## Overall goal

A phone-only personal news app at `https://sameerjadav.github.io/follow-news/`,
generated daily by GitHub Actions and ready before 07:00 IST. Two sections
(World, India) carrying only the genuinely biggest stories, written in plain
adult English from claims individually anchored to sources. Tapping **Follow**
on a story opens a prefilled GitHub issue; the next run researches that story
from its actual beginning, then appends a timeline entry per day until it
closes. Everything runs on free tiers. Done = it runs unattended for a month
without anyone touching it.

## Shared context (applies to every phase)

- **Local path:** `/home/sameer/repos/daily-digest-new` — note the directory
  name does _not_ match the repo name `follow-news`. Remote `origin` is set,
  branch `main`, clean, two commits.
- **Stack:** Python ≥3.12 managed with `uv`. No build step, no bundler, no
  frontend framework. `uv run` should be the entire toolchain. Frontend is
  hand-written `docs/style.css` + `docs/app.js` served as-is, with HTML
  generated from template strings in Python.
- **Architecture invariant (establish in Phase 1, never violate after):**
  `data/YYYY-MM-DD.json` is the single source of truth. Every HTML page is
  _derived_ from it. Generated HTML in `docs/` is overwritten on every render
  and must never be hand-edited. Only the AI pipeline writes `data/`.
- **Models:** a Gemini Flash model for selection, claims and daily writing.
  **Gemini 2.5 Pro with Google Search grounding** for Follow's backstory
  research only. Pin model names in module-level constants so they are changed
  in one place.
- **Quota discipline — the most important engineering constraint.** Free-tier
  RPD is unpublished and was cut 50–80% without notice (`research.md` §3.1).
  Never make one LLM call per article. Select from cheap headlines first, fetch
  full text only for what was chosen, then write. The whole morning should cost
  **3–4 LLM calls**, not hundreds. A design that scales calls with article count
  is wrong regardless of how well it performs today.
- **Diagnostics convention:** a `dbg()` helper printing to **stderr** only,
  never into the site. Keep it verbose — a scheduled run at 02:00 can only be
  debugged afterwards from the Actions log.
- **Dates:** the digest day is computed in IST; everything else is UTC.
- **Code quality bar:** clean, best-practice Python. Flat, one-module-per-concern
  layout; type hints on function signatures; docstrings that explain _why_, not
  _what_. Since this is greenfield, the conventions you establish in Phase 1 are
  the conventions every later phase follows — set them deliberately.
- **Tests:** fragile edges only (settled decision). Each phase tests what it
  built: feed parsing, article extraction, the HTTP-200-with-zero-items case,
  and schema validation of LLM output against saved fixtures. No tests on
  prompts or rendering. Target roughly 200 lines across the whole project.

### The `gh` CLI is available — use it

`gh` is installed and authenticated as **SameerJadav**, with scopes `repo`,
`workflow`, `gist`, `read:org`. Agents should use it directly to verify and
debug rather than guessing or asking. It is the only practical way to inspect a
scheduled run after the fact.

```sh
gh workflow run digest.yml              # trigger manually instead of waiting for cron
gh run list --limit 5                   # recent runs and their status
gh run view <id> --log                  # full log, including all dbg() stderr output
gh run view <id> --log-failed           # just the failing step
gh run watch                            # follow an in-flight run
gh api repos/SameerJadav/follow-news/pages   # confirm Pages is configured
gh secret list                          # confirm GEMINI_API_KEY is set (never prints values)
gh issue list --label follow            # inspect Follow requests (Phase 5)
gh browse                               # open the repo
```

**Verify with `gh` rather than assuming.** After any workflow change, trigger it
and read the log. After the first Pages deploy, confirm the site actually serves
with `curl -I`.

### Secrets and credentials

- The Gemini API key is supplied by the user out-of-band. **It is deliberately
  not written into this plan, into any file in the repo, or into any prompt.**
- Load it into Actions with `gh secret set GEMINI_API_KEY`, and locally via a
  shell export or a **gitignored** `.env`. Confirm with `gh secret list`.
- **The repo is public.** Never echo the key, never write it into `data/`,
  `docs/`, or a log line, and never use `pull_request_target` in any workflow.
- If a key is ever pasted into a chat, a commit, or an issue, treat it as burned
  and rotate it at `https://aistudio.google.com/apikey`.

### Do NOT break

- **`product.md`, `research.md`, `decisions.md` are the spec.** Do not edit them
  and do not silently contradict them. If a phase needs to deviate, stop and ask.
- **Do not add unrequested features.** `product.md` has a "Deliberately left
  out" section — no notifications, no automatic memory or "since you read", no
  accounts, onboarding, personalization or email, no multi-layer story
  structure, no "what to watch next", no quota stories for under-covered
  regions. These are choices, not gaps.
- **Once Phase 1 sets the `data/` contract**, later phases extend it rather than
  reshaping it, and `docs/` stays derived.

### Open decisions for the user

None outstanding. All product and build decisions are settled in `decisions.md`
plus the four recorded during planning: build from scratch; drop audio entirely;
tests on fragile edges only; Flash for the pipeline and Pro for Follow.
Per-phase "decisions to bring to me" items are noted in their own blocks.

## Phases (overview)

1. **Foundation** — uv project, ingestion, extraction, data contract, render, workflows, live on Pages.
2. **Selection** — World/India sections, scope filter, variable story count, Wikipedia cross-check.
3. **Claim-anchored writing** — claims pass, anchored sources, variable length, thin-source flag, Words to Know.
4. **Reading experience** — mobile frontend, tappable sources, pronunciation, archive, hard close, stale banner.
5. **Follow** — issue trigger, grounded backstory, timeline, auto-close.
6. **Hardening & calibration** — 429 resume, degradation, feed health, security, then real-morning tuning.

---

---------- PHASE 1 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 1 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only personal news app published daily to GitHub
Pages — two sections (World, India) of only the biggest stories, written in plain adult
English from claims anchored to individual sources, plus an opt-in Follow feature. Free
tiers only. It must run unattended for a year.

**Where things stand:** `/home/sameer/repos/daily-digest-new` (remote
`SameerJadav/follow-news`, public, branch `main`) contains three markdown documents and no
code: `product.md`, `research.md`, `decisions.md`. Read all three first — they are the
specification. This is a greenfield build; the conventions you set here are the ones every
later phase follows.

**This phase's objective:** Stand up the whole pipeline end-to-end and get a real digest
onto the phone daily. Content _quality_ is explicitly not the goal — plumbing is. By the
end, something real should appear at the Pages URL every morning.

**In scope:**

- `pyproject.toml` (Python ≥3.12), uv project, `.gitignore`.
- A `digest.py` containing: feed ingestion, an article-time window, article text
  extraction, a simple two-pass select→write, the `data/` contract, and rendering.
- `feeds.txt` with the 14 verified feeds (below).
- Minimal readable HTML output — plain single column is fine here.
- Two GitHub Actions workflows plus Pages deployment.
- Tests for feed parsing and article extraction.

**Out of scope:** World/India sections (Phase 2), claim anchoring (Phase 3), any real
frontend design (Phase 4), Follow (Phase 5), resilience hardening (Phase 6). No audio in
any form — it is a settled cut.

**Context you need (already gathered — don't re-explore from scratch):**

- **`feeds.txt` — these 14 were tested working on 2026-07-25** (`research.md` §2.3).
  World: BBC World, Al Jazeera, Guardian World, NPR World, DW, France24, Channel News Asia.
  India: The Hindu National, Indian Express India, NDTV India, Hindustan Times India,
  Times of India top stories, Livemint, Scroll.in.
  Exact URLs are in `research.md` §2.3. Suggested format: one `Name URL` per line, `#` for
  comments, so feeds can be changed without touching code.
  - **Do not add** AP, Reuters, UN News, Deccan Herald national, Business Standard or PIB —
    all tested 401/403/404.
  - **Do not add** The Wire or The Print — they return HTTP 200 with _zero items_. Phase 6
    handles that trap explicitly.

- **Article text extraction — the highest-risk piece, already prototyped.** RSS gives
  headlines and snippets only; article bodies must be fetched. Two strategies were measured
  (`research.md` §2.4), and **neither alone is sufficient**:
  - JSON-LD `articleBody` from `<script type="application/ld+json">` works for the Indian
    outlets (Indian Express 3,075 chars, Hindustan Times 2,626, Times of India 4,001,
    Livemint 3,675) and is **absent** on BBC, Al Jazeera, Guardian and The Hindu.
  - Paragraph extraction (strip script/style/nav, take `<p>` over 60 chars) works for BBC
    (8,461), Al Jazeera (6,596), Guardian (3,603), The Hindu (4,896) — and **fails on Times
    of India**, returning 635 chars of author boilerplate.
  - So: try JSON-LD first, fall back to paragraphs. **Use Times of India as the acceptance
    test** — if it yields under ~1,000 chars, extraction is broken.
  - `trafilatura` is a mature library worth evaluating as the primary extractor, but it was
    not the thing measured above. If you use it, still validate against the TOI case.
  - NDTV returns **403** to a normal User-Agent. `https://r.jina.ai/<url>` fetched it
    successfully, free and keyless at 20 RPM. Wire this in as a last-resort fallback only.

- **Suggested `data/YYYY-MM-DD.json` shape** for this phase (Phases 2–3 extend it):

  ```
  { date, date_label, generated_at,
    stories: [ { headline, body, sources: [{outlet, url}], vocab: [{term, say, meaning}] } ] }
  ```

  `body` is a single continuous piece of prose — significance is woven in, not split into
  labelled sections (`product.md` is explicit about this).
  For `vocab`, `say` is a **phonetic respelling with the stressed syllable capitalised**
  (`sovereignty` → `SOV-rin-tee`), not IPA. `research.md` §5 explains why: the free
  dictionary API gives IPA for only 9 of 12 news-typical words, audio for 4, and fails
  entirely on inflected forms like `sanctions`. Respelling is also directly usable by a
  non-native reader who doesn't read IPA.

- **Two-pass LLM shape (do not deviate — this is the quota strategy):** pass one sends a
  numbered list of `[i] (outlet) title — summary` and returns which article ids cluster into
  which stories. Full text is then fetched _only_ for the chosen articles. Pass two writes
  from that text. Two calls per day, independent of how many articles were ingested.
  Use Gemini structured output: `response_mime_type="application/json"` plus a
  `response_schema`. Output is syntactically valid JSON but semantically unvalidated —
  validate ids against the input range before trusting them.

- **Article window:** only consider articles published since the previous digest, so nothing
  is digested twice. A floor of ~12h (so a manual same-day rerun still has a real window)
  and a cap of ~48h (so a gap doesn't flood the run) is a sane starting point.

- **Workflows:**
  - `digest.yml` — scheduled. **GitHub delays free-tier cron by 1–4 hours in practice and
    its own docs admit queued jobs "may be dropped"** (`research.md` §4.1). Use **three
    staggered cron entries**, all invoking an `--if-missing` mode that no-ops when today's
    JSON already exists, so whichever fires first generates and the rest do nothing. Target
    the first fire around 02:00 IST for an 07:00 IST read — roughly five hours of slack.
    Add `workflow_dispatch` so it can be triggered by hand.
  - `render.yml` — push-triggered, re-renders HTML from `data/` with no API key.
  - Both: commit generated output back, then `upload-pages-artifact` + `deploy-pages`.
    Pages deploys fail transiently — **add a retry with a short sleep**; expect to need it.
  - `permissions: contents: write, pages: write, id-token: write`, and a
    `concurrency: group: pages, cancel-in-progress: false` so runs can't race.
  - Public repo: the key comes from Actions secrets only, and no workflow may use
    `pull_request_target`.

- **CLI shape** worth adopting: bare run = full pipeline; `--if-missing` = no-op if today's
  JSON exists; `render` = re-render everything from `data/` with no API key needed.

- **Use `gh` to verify** rather than assuming: `gh secret set GEMINI_API_KEY`, then
  `gh workflow run digest.yml`, then `gh run watch` and `gh run view <id> --log` to read the
  `dbg()` output. Check the deploy with `gh api repos/SameerJadav/follow-news/pages` and
  `curl -I` against the Pages URL.

- **Gotchas:**
  - `BASE_URL` is `https://sameerjadav.github.io/follow-news/`. The local directory is
    named `daily-digest-new` — do not let that name leak into a URL or hardcoded path.
  - **GitHub Pages must be enabled with source = GitHub Actions.** Check with
    `gh api repos/SameerJadav/follow-news/pages`; if it 404s, tell me — it may need doing in
    the web UI, and the first deploy fails silently without it.
  - `html.escape()` every interpolated value, with `quote=True` inside attributes.

**Do NOT break:** `product.md` / `research.md` / `decisions.md` are the spec — don't edit
them. Repo is public: `GEMINI_API_KEY` in Actions secrets only, never echoed, never in
`data/` or `docs/`, no `pull_request_target`. No audio, no memory features, no "what to
watch next".

**Decisions to bring to me (don't guess):** the module layout — one `digest.py` or several
modules. Show me before implementing; every later phase inherits it. Also tell me which
extraction approach you chose and what it scored on the Times of India acceptance test.

**Definition of done:**

- `GEMINI_API_KEY=... uv run digest.py` writes a valid `data/YYYY-MM-DD.json` and renders
  `docs/index.html` plus an archive.
- `uv run digest.py render` re-renders with no API key.
- `uv run digest.py --if-missing` no-ops when today's file exists.
- Article extraction passes the Times of India test.
- Both workflows committed; `gh workflow run digest.yml` publishes to Pages successfully,
  verified with `gh run view --log` and `curl -I`.
- Tests pass for feed parsing and extraction.
- A real digest is readable at the Pages URL on a phone.

**Hand-off to next phase:** Phase 2 needs working ingestion returning
`{outlet, title, url, summary}` records, a working select→write round-trip it can
restructure, and a `data/`+render pipeline stable enough that changing the JSON shape means
touching only the schema, the prompt, and the renderer.

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 1 ----------

---------- PHASE 2 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 2 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only daily news app on GitHub Pages — two sections
(World, India) of only the genuinely biggest stories, plain adult English, claim-anchored
sources, plus an opt-in Follow feature. Free tiers only.

**Where things stand:** Phase 1 built the pipeline in
`/home/sameer/repos/daily-digest-new` (repo `SameerJadav/follow-news`). It ingests 14 RSS
feeds, selects stories with one Gemini call, fetches article text for the chosen ones,
writes them with a second call, and renders `data/YYYY-MM-DD.json` into `docs/`. It runs
daily on Actions and publishes to Pages. Selection is currently naive — a flat list with no
sections and no real notion of importance.

**This phase's objective:** Make _selection_ right. Split into World and India, enforce the
scope rules, and let the number of stories float with the actual news instead of hitting a
quota.

**In scope:**

- Two sections in the data and in the select pass: `world` and `india`.
- **India-angle wins** (settled): any story with a significant India dimension — trade,
  tariffs, diaspora, borders, Indians abroad — goes in India. World is genuinely
  elsewhere-only. **No story appears in both sections.**
- **Variable story count.** No fixed number. A quiet day may be two or three; a heavy day
  ten. `product.md` explicitly forbids padding, filler, and quota stories added to cover an
  under-reported region — make sure nothing in the prompt encourages any of those.
- **Scope filter.** In: politics, conflict, economy, disasters; major sport (national
  moments only — a World Cup final, not a league fixture); science, health, climate,
  technology. Out: culture, entertainment, obituaries.
- **Prominence signal:** count of _distinct outlets_ covering a cluster, not article volume.
- **Wikipedia Current Events cross-check** as a curated importance check.

**Out of scope:** claim anchoring and prose quality (Phase 3), the section-switching UI
(Phase 4 — this phase only needs the data to carry sections), Follow, resilience.

**Context you need (already gathered — don't re-explore from scratch):**

- **Extend the existing select pass; do not add a second call.** Add a `section` field to
  the selection schema rather than running the pass twice. The two-calls-per-day economy is
  the quota strategy and is not negotiable.
- **Why distinct-outlet count and not volume** (`research.md` §2.5): volume-based ranking
  over-weights whatever outlets happen to churn, so a celebrity story can out-publish a
  coup. Counting how many _different_ outlets carry a cluster is the better free signal.
- **Wikipedia Current Events Portal**, verified working 2026-07-25:
  `https://en.wikipedia.org/api/rest_v1/page/html/Portal:Current_events/YYYY_Month_D`
  (e.g. `Portal:Current_events/2026_July_24`). Returns categorised, human-curated events —
  Armed conflicts and attacks, Politics and elections, Disasters, and so on — each with an
  inline citation to a news outlet. This is the only free _human-curated_ correction to
  volume bias available, and it is the best answer to `product.md`'s "be sure he didn't miss
  anything that matters."
- **Optional extra prominence source:** Google News RSS topic feeds carry a `<source>` tag
  per item naming the outlet, and were measured fresh (median item age 20h, none older than
  46h). Useful for distinct-outlet counting. **Never use it for article text** — its links
  are JavaScript redirects that cannot be resolved by fetch (`research.md` §2.2).
- **Gotchas:**
  - Variable story count is the hardest thing here and has no ground truth. Expect to
    calibrate it in Phase 6 against real mornings — so make the cutoff a **named, tunable
    mechanism**, not a sentence buried in a prompt.
  - Stories genuinely spanning both sections are common (US tariffs on India). State the
    India-angle rule explicitly in the prompt rather than leaving the model to infer it.
  - Scope exclusions need to be stated as rules too; "biggest story" alone will pull in
    celebrity deaths and awards.

**Do NOT break:** the two-calls-per-day economy — no call per story or per section. `data/`
stays the single source of truth and `docs/` stays derived. Don't add memory, follow-ups,
or "what to watch next". Don't edit the three spec documents.

**Decisions to bring to me (don't guess):** how the story-count cutoff is expressed — model
judgement, a distinct-outlet threshold, or a hybrid. Show me the mechanism before building
it.

**Definition of done:**

- `data/YYYY-MM-DD.json` tags every story `world` or `india`, never both.
- Story count visibly varies across at least three days of test runs.
- A day containing a major entertainment story or a celebrity death excludes it.
- The Wikipedia cross-check runs and its effect is visible in `dbg()` output
  (`gh run view <id> --log`).
- Still two LLM calls per day.

**Hand-off to next phase:** Phase 3 needs selected clusters with their member articles and
fetched text, tagged by section and ordered by weight, so it can extract claims and write.

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 2 ----------

---------- PHASE 3 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 3 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only daily news app on GitHub Pages — World and
India sections of only the biggest stories, written in plain adult English from claims
anchored to individual sources, plus an opt-in Follow feature. Free tiers only.

**Where things stand:** Phase 1 built the pipeline; Phase 2 made selection correct —
`data/YYYY-MM-DD.json` now carries a variable number of stories tagged `world` or `india`,
chosen by cross-outlet prominence and cross-checked against Wikipedia Current Events. The
write pass still composes prose freely from source text and attaches a flat list of source
URLs per story.

**This phase's objective:** Replace free composition with **claim-anchored generation**, so
every factual statement traces to one specific source. The product's trust promise lives or
dies here.

**In scope:**

- A **claims pass**: extract atomic claims from fetched article text, each anchored to
  exactly one source URL.
- A **writing pass** composing stories **only from anchored claims**. A claim that cannot be
  anchored never enters the text.
- **Per-claim source markers** in the data — the spans Phase 4 will make tappable.
- **Variable length by weight** (settled): lead story ~500 words, secondary ~200.
- **Plain adult English** (settled): clear and jargon-free but _not_ simplified — closer to
  a good explainer site than a children's news service.
- **Thin-sourcing detection** → a flag driving a **badge at the top of the story** (settled).
- **Words to Know** — 2–6 terms per story with `term` / `say` / `meaning`.
- Schema-validation tests of LLM output against saved fixtures.

**Out of scope:** rendering markers and the badge (Phase 4 — produce the data here), Follow,
resilience.

**Context you need (already gathered — don't re-explore from scratch):**

- **Why this design, from `research.md` §6:** post-hoc attribution — writing first, then
  asking which source supports each sentence — produces attributions that are "coarse and
  generated post hoc, making each summary statement hard to verify"
  (_Faithful by Construction_, arXiv 2606.23989). That is exactly the "trust theater"
  `product.md` forbids when it says visible trust signals must never run ahead of actual
  accuracy. Extracting and anchoring claims _before_ writing makes attribution a property of
  construction rather than a second guess.
- **Two trust rules this makes mechanical rather than judged:**
  - Numbers are never averaged or blended across sources. A figure lives on exactly one
    claim with exactly one source; two sources disagreeing produces **two claims**, not one
    averaged figure. `product.md` calls this out directly.
  - Thin sourcing is a _measured_ low distinct-outlet count across a story's claim set, not
    a model assessment.
- **Writing rules to encode in the prompt:** neutral attribution ("the ministry says",
  "witnesses told the BBC"); never present a contested claim as settled; state explicitly
  where outlets disagree or facts are uncertain; short sentences, active voice, concrete
  nouns; enough background that a first-time reader needs no prior digest — this is
  `product.md`'s "every story stands alone". Significance is **woven into the body**, never
  split into a labelled section.
- **Structured output:** Gemini guarantees syntactically valid JSON against a
  `response_schema` but explicitly not semantic correctness — "always validate values in
  your application." Validate that every claim id referenced by the prose exists and falls
  within the input range.
- **Gotchas:**
  - Strict anchoring and natural prose pull against each other. The failure mode is prose
    that reads like facts stapled together. This is the single biggest quality risk in the
    project — budget real iteration, and read the output as a reader, not as a developer.
  - Adding a third LLM call is fine (3–4/day total). Adding a call _per story_ is not.
  - Some articles will be RSS-summary-only (extraction is best-effort; NDTV 403s). Claims
    from summary-only sources are weaker — instruct the model to stick to what a summary
    supports and not extrapolate beyond it.

**Do NOT break:** total LLM calls stay at 3–4/day. `data/` remains the single source of
truth. Don't reintroduce labelled story sections, memory, or "what to watch next". Repo is
public — no secrets in output. Don't edit the three spec documents.

**Decisions to bring to me (don't guess):** the claim→text linking representation —
character offsets, inline markers, or claim ids per paragraph. This determines what Phase 4
can build, so show me the shape first. Also bring me the thin-sourcing threshold before
hardcoding it.

**Definition of done:**

- Every story's prose traces to claims; every claim to exactly one source URL.
- A story built from a single outlet is flagged thin-sourced in the data.
- Lead stories are visibly longer than secondary ones.
- Read three real days of output: the prose reads as continuous writing, not a list of
  attributed facts.
- Schema-validation tests pass against saved fixtures.

**Hand-off to next phase:** Phase 4 needs, per story: body text, a resolvable mapping from
spans of that text to source URLs and outlet names, a thin-sourced boolean, the vocab list,
and the section tag.

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 3 ----------

---------- PHASE 4 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 4 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only daily news app on GitHub Pages — World and
India sections of the biggest stories, plain adult English, claim-anchored sources, plus an
opt-in Follow feature. Free tiers only.

**Where things stand:** Phases 1–3 produce a complete, correct `data/YYYY-MM-DD.json` every
morning: stories tagged `world`/`india`, variable in number and length, composed from claims
anchored to individual sources, with thin-sourcing flags and a vocab list. Rendering is
still the plain single-column page from Phase 1.

**This phase's objective:** Build the reading experience. This is a **phone-only** app for
one reader on **Android**, read for about ten minutes at breakfast. The phone is the only
screen this is designed for.

**In scope:**

- **Two sections you switch between** — World and India. Not one long mixed list.
- **Story presentation:** informative headline and opening line carrying the gist, so
  skimming still means not missing anything. The rest complete but concise. Significance
  woven in — no labelled sections.
- **Tappable source markers** on claims — small and unobtrusive, revealing which outlet a
  fact came from without cluttering the prose.
- **Thin-sourced badge** at the top of affected stories.
- **Words to Know** with tap-to-hear pronunciation.
- **A hard close** — a clear, finite end after the day's stories. No endless scroll, no
  pretending there's always more.
- **Archive** — full accessible archive, browsable as a date list, newest first.
- **Stale banner** — when today's digest hasn't landed, show yesterday's with an honest note
  ("Today's isn't ready yet — last updated 6am"), replaced automatically when the run lands.
  Never a blank screen, never a half-built digest presented as complete.

**Out of scope:** the Follow button and its issue link (Phase 5 — leave a clear seam), the
run-side logic that decides staleness (Phase 6 — render the banner when the data says so),
resilience.

**Context you need (already gathered — don't re-explore from scratch):**

- **Pronunciation via the Web Speech API** (`research.md` §5). This is the entire
  pronunciation feature — free, offline, on-device, no API:
  - On mobile, `speak()` **only fires inside a user-gesture handler** (a tap) or WebKit
    silently drops the utterance. Tap-to-hear fits this exactly — never try to autoplay.
  - `getVoices()` can return empty on first call; listen for `voiceschanged` once and retry.
  - Android supports voice selection properly, which is why Android was the chosen target.
    Setting `lang` to `en-IN` and a slightly slowed rate (~0.85) suits a non-native reader.
- **Phonetic respellings** come from the data as `say` (e.g. `SOV-rin-tee`) and are shown as
  text next to each term — they are useful on their own, without audio.
- **Design constraints:** mobile-first, no horizontal scroll, generous tap targets, works in
  light and dark. Include a PWA manifest and icons so it can be added to the home screen —
  but **no notifications**, ever; `product.md` rules them out explicitly.
- **Rendering approach:** HTML generated from Python template strings; `docs/style.css` and
  `docs/app.js` are hand-written, served as-is, and never rewritten by the pipeline. No
  build step, no bundler, no framework.
- **Gotchas:**
  - `html.escape()` every interpolated value, `quote=True` inside attributes.
  - After editing a template string you must re-render to see the change — editing generated
    HTML in `docs/` directly is pointless, it gets overwritten.
  - Avoid any shared helper that must be kept identical between Python and JavaScript (e.g. a
    slug function on both sides). That class of duplication breaks silently.
  - Test on a real phone, not just a narrow desktop window. `gh browse` opens the repo;
    the Pages URL is `https://sameerjadav.github.io/follow-news/`.

**Do NOT break:** `data/` stays the single source of truth and every page stays derived from
it. The no-API-key render path must keep working. No build step, no framework. No
notifications. Don't edit the three spec documents.

**Decisions to bring to me (don't guess):** the visual direction, before you write CSS — I
want that chosen deliberately rather than defaulted into. Also how section switching works
(tabs, swipe, or stacked sections with a jump control), and how a tapped source marker
reveals its outlet (inline expand, bottom sheet, or footnote jump).

**Definition of done:**

- Comfortable to read on an Android phone at breakfast; no horizontal scroll.
- World and India are genuinely separate views, switchable.
- Tapping a source marker reveals the outlet for that specific claim.
- Tapping a Words-to-Know term speaks it aloud.
- The digest ends with a clear, finite close.
- The archive lists every past day, newest first.
- Rendering a `data/` file whose date isn't today produces the stale banner.
- Light and dark both work.

**Hand-off to next phase:** Phase 5 needs a clear place in the story template for the Follow
button, and a rendering path for followed-story pages (backstory + timeline) that reuses
this phase's typography, layout and source-marker component.

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 4 ----------

---------- PHASE 5 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 5 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only daily news app on GitHub Pages — World and
India sections of the biggest stories, plain adult English, claim-anchored sources, plus an
opt-in Follow feature. Free tiers only.

**Where things stand:** Phases 1–4 deliver a complete daily digest: correct selection,
claim-anchored writing, and a finished mobile reading experience with archive and stale
banner. Phase 4 left a seam in the story template for a Follow button and a rendering path
for followed-story pages.

**This phase's objective:** Build **Follow** — what `product.md` calls "the heart of the
app" and "the actual reason to open the app daily, not the digest."

**In scope:**

- **Follow button** → opens a **prefilled GitHub issue** (settled). The site is static on a
  public repo, so it holds no secret and has no write endpoint; Follow rides on GitHub's own
  auth.
- **A workflow triggered on issues** that reads the request and queues the story.
- **Full-picture explainer**: research the story from wherever it actually began — even
  months before it appeared in the digest — using **Gemini 2.5 Pro with Google Search
  grounding**. The reader must never be dropped into the middle with missing backstory.
- **Timeline**: each subsequent day appends one entry, growing the picture rather than
  repeating it.
- **Auto-close after ~14 days with no significant development** (settled), with a final
  entry. **No cap** on active follows.
- **Search Suggestions chips** rendered as the grounding Terms require.
- A `followed/` data contract paralleling `data/`.

**Out of scope:** resilience hardening and calibration (Phase 6).

**Context you need (already gathered — don't re-explore from scratch):**

- **Security requirement — the one security-relevant detail in the whole project.** The repo
  is public, so _anyone_ can open an issue. The workflow must act only on issues authored by
  the repo owner and ignore everything else: check
  `github.event.issue.user.login == 'SameerJadav'`. Without it, a stranger can drive your
  pipeline and burn your quota. Test this by reasoning through a non-owner issue; verify the
  guard with `gh run view <id> --log`.
- **Grounding with Google Search** (`research.md` §3.1, from Google's own docs): free-tier
  quota is **1,500 RPD** on Gemini 2.5 models or **5,000 prompts/month** on Gemini 3. It
  returns `url_citation` annotations carrying source URL, title, and `start_index` /
  `end_index` offsets into the generated text — the same shape Phase 3 uses for claim
  anchoring, so Phase 4's source-marker component should be reusable directly. The
  **Terms of Service impose display requirements**: the response includes a
  `search_suggestions` HTML snippet that must be rendered. Read
  `https://ai.google.dev/gemini-api/terms#grounding-with-google-search` before choosing
  placement.
- **Prefilled issue URL** form:
  `https://github.com/SameerJadav/follow-news/issues/new?title=...&body=...&labels=follow`
  — URL-encode properly, and embed enough identity (date plus story slug or headline) that
  the workflow can resolve which story is meant without ambiguity.
- **Timeline quality risk** (`research.md` §6): 2026 work on news timeline summarization
  (NTS-CoT, arXiv 2606.13171) finds timelines have a failure mode beyond unfaithfulness —
  **information omission** in date-event summaries. The timeline needs a completeness check,
  not only a correctness check.
- **Useful `gh` commands here:** `gh issue list --label follow`,
  `gh issue view <n> --json author,title,body`, `gh issue create` to test the flow end to
  end, and `gh run view <id> --log` to confirm the owner guard fired.
- **Gotchas:**
  - `product.md` is emphatic: **nothing follows itself.** No story is ever tracked, updated
    or resurfaced unless deliberately followed. Do not add heuristics that auto-follow big
    stories.
  - Follows are uncapped, so timeline appends must stay cheap — batch across all active
    follows rather than one call per followed story per day.
  - Generate a backstory **once**. Regenerating it daily burns quota and breaks the "grows
    the fuller picture you already have" promise.
  - The issue-triggered workflow and the daily cron workflow both write to the repo and can
    race. Make sure they cannot clobber each other.

**Do NOT break:** the daily digest must keep publishing even if Follow fails entirely —
Follow is additive and never on the digest's critical path. Batch, don't loop per story.
`data/` and `followed/` stay the sources of truth with pages derived. Repo is public — key
in Actions secrets only, no `pull_request_target`. Don't edit the three spec documents.

**Decisions to bring to me (don't guess):** where the Search Suggestions chips go — I chose
to show them but not their placement, and it affects the reading experience. Also the
`followed/` data shape and how a follow request resolves to a story, before implementing.

**Definition of done:**

- Tapping Follow opens a correctly prefilled GitHub issue.
- Submitting it causes the next run to produce a followed-story page whose backstory
  genuinely predates the story's first appearance in the digest.
- An issue opened by any account other than the owner is ignored.
- A second day of news appends a timeline entry without regenerating the backstory.
- A story with no development for 14 days closes with a final entry.
- Search Suggestions render wherever we agreed.
- The daily digest still publishes normally with Follow active.

**Hand-off to next phase:** Phase 6 needs both the digest and Follow paths complete, so
retry, degradation and health checks can cover both together.

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 5 ----------

---------- PHASE 6 of 6 — COPY EVERYTHING BELOW INTO A NEW PLAN-MODE SESSION ----------

You are in a fresh Claude Code session in plan mode. This is Phase 6 of 6 of a larger
effort. The full meta plan lives at `/home/sameer/repos/daily-digest-new/meta-plan.md` — read it if you need the complete
picture, decisions made in other phases, or context beyond what's below. Make a detailed
plan for THIS phase and wait for my approval before implementing.

**Overall goal (for context):** A phone-only daily news app on GitHub Pages — World and
India sections of the biggest stories, plain adult English, claim-anchored sources, and an
opt-in Follow feature. Free tiers only.

**Where things stand:** Phases 1–5 deliver the complete product: daily digest with correct
selection and claim-anchored writing, a finished mobile reading experience, and Follow with
grounded backstories and timelines. It works. It is not yet hardened.

**This phase's objective:** Make it survive a year with nobody maintaining it — then
calibrate its judgement against real mornings. `product.md` states this is "built once and
then left alone… no one's coming back to patch it," so the reliability bar is higher than a
normal side project.

**In scope — hardening:**

- **429 wait-and-resume.** `product.md` requires that hitting a free-tier limit "must never
  break the morning: wait for the limit to lift and resume, rather than failing." A fixed
  number of retries with a flat sleep is not the same thing. Distinguish rate-limit errors
  from real failures and wait out the window.
- **Degrade, don't fail.** A digest built from 6 of 14 sources still ships. Replace any hard
  "too few articles" exit with a quorum model.
- **Feed health checks that catch silent decay.** Status codes are not enough: The Wire and
  The Print both return **HTTP 200 with zero items**, and 6 of the 22 feeds originally tested
  were already dead or blocked on day one (`research.md` §2.3, §7.1). Over a year of no
  maintenance more will rot. Detect and report degradation rather than discovering it after
  a bad morning.
- **Stale-banner wiring** — the run-side logic deciding today's digest hasn't landed, which
  Phase 4 renders.
- **`noindex`** — `robots.txt` plus meta / `X-Robots-Tag`. GitHub Pages does not do this for
  you, and `product.md` promises the site "asks search engines not to index it," so it must
  be actively set. Verify with `curl -I`.
- **Secrets hygiene audit** across both workflows: key never echoed, never in output, no
  `pull_request_target`. Confirm with `gh secret list` and by reading a full run log.
- **A `CLAUDE.md`** for the repo: commands, architecture, the invariants, the gotchas. This
  is what makes the project resumable a year from now, and it is the deliverable that most
  directly serves "built once, left alone."

**In scope — calibration (repeat over several real mornings):**

- Read the actual digest each morning and tune: was the story count right? Was anything
  genuinely big missed? Did the World/India split feel right? Does the prose read as writing
  rather than stapled facts? Are the Words to Know the right words?
- Tune prompts and the selection cutoff against those observations, not against intuition.

**Out of scope:** new features. If something in `product.md` isn't built by now, bring it to
me rather than adding it.

**Context you need (already gathered — don't re-explore from scratch):**

- **Free-tier volatility** (`research.md` §3.1): Google no longer publishes per-model
  free-tier RPD — the docs say only that limits "can be viewed in Google AI Studio."
  Third-party reports range 250–1,500 RPD, and quotas were cut 50–80% in December 2025.
  Check the real number in AI Studio now that the key has usage history, and size backoff to
  what you actually observe.
- **Cron reality** (`research.md` §4.1): GitHub's docs admit scheduled runs can be delayed
  under load and that "some queued jobs may be dropped"; delays of 1–4 hours are commonly
  reported. The staggered `--if-missing` crons from Phase 1 handle this — verify the
  idempotency actually holds under a double-fire by triggering two runs close together with
  `gh workflow run` and reading both logs.
- **Fallback capacity if Gemini is exhausted** (`research.md` §3.2): Groq's free tier is
  per-organization, and the binding constraint is **tokens/day, not requests/day** —
  `llama-3.3-70b-versatile` allows 100K TPD, roughly 25–35 full articles of input. Viable
  for a reduced digest, not a peer replacement. Only build this if real quota data says it's
  needed.
- **Jina Reader fallback:** `https://r.jina.ai/<url>` successfully fetched an NDTV article
  that returns 403 to a normal User-Agent. Free at **20 RPM keyless**. Worth wiring in as a
  last-resort text source if NDTV or similar misses show up frequently in the logs.
- **Debugging scheduled runs** is only possible after the fact — `gh run list`,
  `gh run view <id> --log`, `gh run view <id> --log-failed`. This is why `dbg()` goes to
  stderr and stays verbose.
- **Gotchas:**
  - Silent decay is the real danger, not loud failure. A digest that quietly drops to four
    sources still looks fine on the page.
  - Don't over-engineer the fallback chain. Every fallback is more code to rot, and
    simplicity is an explicit product constraint.

**Do NOT break:** everything Phases 1–5 built must keep working — hardening must not regress
behaviour. `data/` and `followed/` stay the sources of truth. Repo is public: no secrets in
output, no `pull_request_target`. Don't edit `product.md`, `research.md` or `decisions.md`.

**Decisions to bring to me (don't guess):** whether to build the Groq fallback at all —
decide from real quota data and tell me what the data says. Also bring me any prompt change
that alters editorial judgement (story count, section split, scope) rather than applying it
silently.

**Definition of done:**

- A simulated 429 causes the pipeline to wait and resume, not fail.
- Disabling half the feeds still produces a digest, with the degradation logged.
- A feed returning HTTP 200 with zero items is detected and reported.
- `robots.txt` and noindex are in place, verified with `curl -I`.
- `CLAUDE.md` exists and is accurate.
- Tests pass.
- **The digest has run unattended and correctly for at least seven consecutive mornings.**

**Note on execution:** the plan you produce will likely be executed by a smaller, cheaper
model. Make each step explicit and self-contained — spell out file paths, names, and the
shape of each change so execution doesn't require re-deriving your reasoning.

---------- END PHASE 6 ----------
