# Follow — Build Research

All findings below were verified on **2026-07-25**, either by fetching primary
documentation or by hitting the live endpoint from this machine. Where a number
comes from a blog rather than a primary source, it says so. Where I tested it
myself, it says **[tested]**.

---

## 1. Verdict

The product is buildable entirely on free tiers. Nothing in it requires a paid
service. Three things in the spec are in genuine tension with reality and are
discussed in §7:

1. "Built once, then left alone, never maintained" vs. a pipeline that depends
   on other people's websites.
2. "Ready before breakfast, guaranteed" vs. GitHub Actions' scheduling, which
   is routinely late and occasionally drops runs.
3. "Free tier limits must never break the morning" vs. LLM free-tier quotas
   that change without notice.

All three are solvable, but they change the design, so they must be decided
before any code is written.

---

## 2. News ingestion

### 2.1 What's ruled out

**NewsAPI.org free (Developer plan)** — unusable. Primary source: their pricing
page states 100 requests/day, **articles have a 24-hour delay**, CORS is
localhost-only, and the plan "may be used for development and testing in a
development environment only, and cannot be used in a staging or production
environment (including internally)." A 24-hour delay alone kills a morning
digest.

**GDELT DOC 2.0 API** — free and keyless, but **[tested]** it returned HTTP 429
on the first request with the message: *"Please limit requests to one every 5
seconds."* It's a soft throttle rather than a hard block, so it is usable as a
low-volume signal, but it can't be a primary corpus. Its real value is breadth
of outlets, not article text.

### 2.2 Google News RSS — good signal, unusable links

**[tested]** `news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en`
returned HTTP 200, 52 items, **median item age 20.0 hours, oldest 46 hours,
34/52 within 24h**. A widely-cited July 2026 blog post claims Google News RSS
has a "median item age of about 6.6 days" — that is **not true** for topic
feeds; I measured it directly.

It also carries a `<source>` tag per item giving the outlet name (The Hindu,
NDTV, Al Jazeera, Hindustan Times…), which is a free cross-outlet prominence
signal: *how many distinct outlets are covering this right now* is exactly the
"is this one of the biggest stories" measure the product needs.

**The blocker:** item links are opaque
`news.google.com/rss/articles/CBMi...` URLs. **[tested]** following one with
`curl -IL` ends at the same Google URL with HTTP 200 — it is a **JavaScript
redirect, not an HTTP redirect**, so it cannot be resolved with a plain fetch.
Decoding it requires an undocumented `batchexecute` call that Google has broken
before without notice.

**Conclusion:** use Google News RSS as a *ranking and corroboration signal*
(headline + outlet + timestamp), never as a source of article text. Get text
from publisher feeds instead.

### 2.3 Publisher RSS — the real corpus

**[tested]** 22 feeds. Results:

| Feed | HTTP | Items | Median age (h) |
|---|---|---|---|
| BBC World | 200 | 26 | 15.9 |
| Al Jazeera (all) | 200 | 25 | 7.4 |
| Guardian World | 200 | 45 | 16.6 |
| NPR World | 200 | 10 | 21.6 |
| DW World | 200 | 13 | n/a |
| France24 | 200 | 24 | 15.3 |
| Channel News Asia | 200 | 20 | 6.7 |
| The Hindu (National) | 200 | 60 | fresh |
| Indian Express (India) | 200 | 200 | 65.2 |
| NDTV India | 200 | 20 | fresh |
| Hindustan Times (India) | 200 | 100 | 9.1 |
| Times of India (top) | 200 | 44 | 5.1 |
| Livemint | 200 | 35 | 0.6 |
| Scroll.in | 200 | 100 | 48.1 |

Dead or blocked: **AP** (403 via RSSHub), **Reuters** (401 — Reuters has no
usable public RSS any more), **UN News** (404), **Deccan Herald** national feed
(404), **Business Standard** (403), **PIB** government feed (403). The Wire and
The Print returned 200 but **zero items** — their feeds are present but empty.

That last category is the important one: *a feed can return HTTP 200 and
silently contain nothing.* Any health check that only looks at status codes will
not notice. This matters a lot given "built once, left alone."

All feeds are headline+snippet only. None ship full article text except
Scroll.in.

### 2.4 Article text extraction — this works

**[tested]** two strategies against live articles:

**Strategy A — JSON-LD `articleBody`.** Indian outlets embed the full article
in `<script type="application/ld+json">`:

| Outlet | chars extracted |
|---|---|
| Indian Express | 3,075 |
| Hindustan Times | 2,626 |
| Times of India | 4,001 |
| Livemint | 3,675 |
| BBC / Al Jazeera / Guardian / The Hindu | 0 (no JSON-LD body) |

**Strategy B — paragraph extraction** (strip script/style/nav, take `<p>` with
>60 chars):

| Outlet | chars | quality |
|---|---|---|
| BBC | 8,461 | clean |
| Al Jazeera | 6,596 | some share-widget noise at top |
| Guardian | 3,603 | clean |
| The Hindu | 4,896 | prefixed with subscription boilerplate |
| Indian Express | 4,192 | clean |
| Hindustan Times | 4,264 | clean |
| Livemint | 5,983 | clean |
| **Times of India** | **635** | **fails** — got author bio only |

**Conclusion:** try JSON-LD first, fall back to paragraph extraction. Together
they cover every outlet tested. Neither alone does — TOI fails on B (635 chars
of author boilerplate), the Western outlets fail on A.

**Bot-blocking fallback.** NDTV returned **403 Forbidden** to a normal browser
User-Agent. **[tested]** `https://r.jina.ai/<url>` fetched the same NDTV article
successfully as clean markdown, with title and published time. Jina Reader is
free: **20 RPM keyless, 500 RPM with a free API key**. It's the right escape
hatch for blocked domains — but it's a third-party dependency on the critical
path, so it should be a fallback, not the default.

### 2.5 Wikipedia Current Events Portal — the "did I miss anything" check

**[tested]** `en.wikipedia.org/api/rest_v1/page/html/Portal:Current_events/2026_July_24`
returned 200 and a structured, human-curated list of the day's significant
events, grouped by category (Armed conflicts and attacks, Politics and
elections, Disasters, …), each with an inline citation to a news outlet.

This is the single best free answer to the product's *"be sure he didn't miss
anything that matters"* requirement, because it is **editorially curated by
humans** rather than inferred from article volume. Volume-based ranking
over-weights whatever outlets happen to churn — a celebrity story can out-publish
a coup. Using the Wikipedia portal as a cross-check against the volume signal
catches exactly that failure.

---

## 3. LLM

### 3.1 Gemini (primary)

From the official pricing page: the free tier covers **Gemini 3.6 Flash, 3.5
Flash, 3.5 Flash-Lite, 3.1 Flash-Lite, 3 Flash Preview, 2.5 Flash, 2.5
Flash-Lite, 2.5 Pro, Gemini Embedding, and Gemini Embedding 2** — including a
capable reasoning model (2.5 Pro) and embeddings, at no cost.

**Two things to know:**

1. **The exact free-tier RPD is no longer published.** The official rate-limits
   page now says only: *"Rate limits depend on a variety of factors… and can be
   viewed in Google AI Studio."* Third-party numbers for 2.5 Flash range from
   **250 RPD to 1,500 RPD** depending on the month they were written; several
   note Google cut free quotas 50–80% in December 2025. **We cannot design
   against a known number.** The design must be quota-agnostic (see §6.1).

2. **Free-tier content is used to improve Google's products.** The pricing page
   marks "Content used to improve our products" as **Yes** for free tier, **No**
   for paid. Everything we send is public news text, so there's no secret
   material — but the *followed-story list* is a signal about the reader, and it
   goes to Google. This is the same category of trade the spec already accepts
   for unlisted hosting, but it wasn't stated there.

**Grounding with Google Search** — free tier gives **1,500 RPD** on Gemini 2.5
models, or **5,000 prompts/month** on Gemini 3 models. It returns
`url_citation` annotations carrying source URL, title, and `start_index` /
`end_index` character offsets into the generated text. This is a very big deal
for two features:

- **Follow's full-picture explainer** ("research the entire story from wherever
  it began") — that is a research task, and grounding does it natively.
- **Tappable per-claim sources** — the citation offsets map generated spans to
  source URLs, which is precisely the data model the UI needs.

Caveat: the Terms of Service impose **display requirements** — if you use
grounding you are required to render the returned Search Suggestions chips.
That is a real constraint on a minimalist reading UI and needs a decision.

**Structured output** is supported via `responseSchema` / `responseMimeType:
application/json`, covering enums, required fields, `minItems`/`maxItems`,
nested objects. The docs are explicit: output is **syntactically** valid JSON,
but *"always validate values in your application"* — schema conformance is not
semantic correctness. Very large or deeply nested schemas may be rejected.

### 3.2 Fallbacks when Gemini 429s

The spec requires that hitting a limit "must never break the morning."

**Groq free tier** (exact figures from their rate-limit docs):

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| llama-3.3-70b-versatile | 30 | 1,000 | 12K | 100K |
| openai/gpt-oss-120b | 30 | 1,000 | 8K | 200K |
| qwen/qwen3.6-27b | 30 | 1,000 | 8K | 200K |
| llama-3.1-8b-instant | 30 | 14,400 | 6K | 500K |

Limits are **per organization, not per key** — extra keys don't help. The
binding constraint is **tokens/day, not requests/day**: 100K TPD on the 70B
model is roughly 25–35 full news articles of input. Groq is a viable *fallback*
for a reduced digest, not a peer replacement.

**Cloudflare Workers AI** — 10,000 neurons/day free, resets 00:00 UTC. Neurons
≈ tokens-equivalent pricing units; small models are cheap (Llama 3.2-1B ≈ 2,457
neurons per million input tokens), embeddings cheaper (BGE-small ≈ 1,841 per
million). Useful for **embeddings and cheap classification**, not for writing.
On the Free plan, exceeding it fails hard rather than billing.

---

## 4. Hosting, compute, delivery

### 4.1 Compute — GitHub Actions

- **Public repo: unlimited free minutes. Private repo (Free plan): 2,000
  minutes/month**, 500 MB artifact storage. A 20-minute daily run is ~600
  min/month — comfortably inside the private-repo allowance.
- **Scheduled workflows run only on the default branch, latest commit.**
- **Minimum interval 5 minutes.**
- **Scheduling is unreliable, and the docs admit it:** *"The `schedule` event
  can be delayed during periods of high loads… If the load is sufficiently high
  enough, some queued jobs may be dropped."* Community reports through 2026
  describe 5–30 minute delays as normal, 50–60 minutes not unusual, and one
  July 2026 thread reports 8–14 hour delays with one day dropped entirely.
- **Public repos have scheduled workflows auto-disabled after 60 days of
  inactivity.** A daily job that commits its output counts as activity, so this
  is self-solving here — but only as long as the job keeps succeeding.

### 4.2 Delivery — where the site lives

**GitHub Pages is a problem on the free plan:** Pages is available for private
repos only on Pro or above. On Free, **Pages requires a public repository**.
That is materially worse than the "unlisted" trade the spec accepts: an
unlisted URL is unguessable, but a **public GitHub repo is searchable on
GitHub** — the followed-story list, the digest history, and the prompts would
all be discoverable, not merely reachable-if-you-had-the-link.

**Cloudflare is the better fit.** Verified free limits:

| | Free limit |
|---|---|
| Workers requests | 100,000/day |
| Workers CPU time | 10 ms per invocation (wall-clock waiting on fetch not counted) |
| Workers cron triggers | 5 per account |
| Subrequests per invocation | 50 |
| **Static asset requests** | **free and unlimited** (explicitly: *"Requests to static assets are free and unlimited"*) |
| Workers KV | 100,000 reads/day, **1,000 writes/day**, 1 GB, 25 MiB/value |
| Pages builds | 500/month, 20,000 files, 25 MiB/file |

For new projects in 2026 Cloudflare recommends **Workers with static assets**
over Pages — as of March 2026 Workers has feature parity for static assets and
custom domains, and every new platform feature ships there first. Pages is not
deprecated, but it's the legacy path.

This means **one Worker can serve the whole thing**: static digest files (free,
unlimited) plus a tiny `/api/follow` endpoint (counts against 100k/day, which
one reader will never approach). KV's **1,000 writes/day** is the only tight
number and is irrelevant for one user tapping Follow occasionally.

**noindex:** Cloudflare adds `X-Robots-Tag: noindex` automatically to *preview*
deployments only. A production `.workers.dev` / `.pages.dev` URL is **not**
noindexed by default — it must be set explicitly via a `_headers` file or
response header, plus `robots.txt`. The spec's "asks search engines not to
index it" is therefore a thing we must actively do, not something we get.

### 4.3 Toolchain present on this machine

Node v24.16.0, Python 3.14.4, git 2.53.0. Repo is empty apart from `product.md`.

---

## 5. Words to know / pronunciation

**[tested]** `dictionaryapi.dev` (free, keyless) across 12 news-typical words:

| | result |
|---|---|
| IPA present | 9/12 (`tariff`, `indictment`, `coalition`, `referendum`, `inflation`, `sovereignty`, `impeachment`, `moratorium`, `diaspora`) |
| IPA missing | 3/12 (`sanctions`, `ceasefire`, `extradition`) |
| Audio present | 4/12 |

Note `sanctions` failed while the lemma `sanction` would succeed — **the API
does not lemmatise**, so inflected forms as they appear in news text fail
often. Audio coverage at 33% is too low to build a feature on.

**Recommended instead:** have the LLM emit a **phonetic respelling**
(`SANK-shunz`) rather than IPA. For a non-native reader, `SANK-shunz` is
directly usable; `/ˈsæŋkʃənz/` requires already knowing IPA. Pair it with the
browser's built-in `speechSynthesis` for tap-to-hear.

**Web Speech API constraints:** supported on iOS Safari, works **offline** using
on-device voices, and costs nothing. Two caveats: on mobile WebKit, `speak()`
**only fires inside a user-gesture handler** (a tap) or the utterance is
silently dropped — which fits tap-to-hear exactly; and Safari's `getVoices()`
is unreliable, so we cannot choose a specific voice and must accept the device
default.

---

## 6. How the accuracy requirement changes the design

The spec's strongest constraint is *"the visible trust signals are never allowed
to run ahead of the actual accuracy behind them."* Current research says the
naive approach fails exactly this test.

**Post-hoc attribution does not work.** The standard pipeline — write a summary,
then ask a model which source supports each sentence — produces attributions
that are, per *Faithful by Construction: Claim-Anchored Attribution for
Multi-Document Summarization* (arXiv 2606.23989), "coarse and generated post
hoc, making each summary statement hard to verify." That is trust theater: the
markers look authoritative and are not.

**The alternative is claim-anchored generation.** Extract atomic claims from
sources *first*, anchor each to the specific document supporting it, and then
write the story **only from anchored claims** — a claim that cannot be anchored
never enters the text. Attribution is then a property of construction rather
than an afterthought, and "which outlet did this fact come from" is answerable
by definition rather than by a second guess.

This maps cleanly onto the spec's other trust rules:

- *"Never guess or blend numbers from different sources"* — a number lives on
  exactly one claim with exactly one source; two sources disagreeing produces
  two claims, not an average.
- *"If a story is only thinly sourced, that's shown honestly"* — thin sourcing
  is just a low distinct-outlet count on the claim set, which is measurable
  rather than judged.

Related 2026 work (NTS-CoT, arXiv 2606.13171) additionally finds that timeline
summarization — precisely the Follow feature — has a second failure mode beyond
unfaithfulness: **information omission** in date-event summaries. So Follow
needs a completeness check, not just a correctness check.

### 6.1 Quota-agnostic pipeline shape

Because free-tier RPD is unknown and unstable (§3.1), the pipeline must not
scale request count with article count. Concretely: **do not make one LLM call
per article.** Batch clustering and claim extraction into a few large calls, so
the whole morning costs on the order of 10–30 requests rather than 200. That
survives a 250 RPD floor and a 1,500 RPD ceiling identically, and it makes the
"wait for the limit to lift and resume" requirement cheap to honour.

---

## 7. The three real risks

**7.1 "Built once, left alone" vs. scraping other people's sites.**
This is the biggest threat to the product, and it's structural, not a bug to be
avoided. Of 22 feeds tested today, 6 were already dead or blocked and 2 returned
200 with zero items. That's the *starting* state. Over a year of no maintenance,
more will rot. Mitigations: (a) heavy source redundancy so any single failure is
invisible; (b) treat a source set as a pool with a quorum, not a list of
required inputs; (c) **degrade rather than fail** — a digest built from 6 of 14
sources is still a digest; (d) alert-on-degradation, because silent decay is
the actual danger, and a 200-with-zero-items feed proves status codes aren't
enough. Search-grounded retrieval is inherently more durable than fixed
scrapers, which argues for leaning on it where quota allows.

**7.2 "Ready before breakfast" vs. GitHub Actions cron.**
Documented as delayable and droppable; reports of multi-hour delays in 2026.
Mitigation: run the pipeline **hours before** the read time (e.g. 03:00 IST for
an 08:30 read), schedule **multiple cron entries** as retries, and make the job
**idempotent** so a duplicate run is harmless. Critically, delivery is a static
file: if today's run is late or dropped, yesterday's digest is still sitting
there. The failure mode should be "today's isn't ready yet, here's why" — never
a blank screen or, worse, a half-built digest.

**7.3 Free-tier drift.**
Google cut free quotas 50–80% in one step in December 2025 and stopped
publishing per-model free limits entirely. Any design that assumes today's
numbers will hold is fragile. §6.1's batching is the main defence; a Groq
fallback path for a reduced digest is the second.

---

## 8. Architecture

Decided 2026-07-25 — see `decisions.md` for the full decision record and the
trades that were accepted. The shape below reflects those decisions.

```
02:00 IST  GitHub Actions (public repo — unlimited free minutes)
           │
           ├─ 1. Gather      publisher RSS (≈14 feeds, quorum-based)
           │                 + Google News RSS (prominence signal only)
           │                 + Wikipedia Current Events (curated cross-check)
           ├─ 2. Rank        distinct-outlet count × recency, cross-checked
           │                 against Wikipedia portal; no fixed story count
           ├─ 3. Fetch text  JSON-LD articleBody → <p> fallback → r.jina.ai
           ├─ 4. Claims      batched extraction, each claim anchored to one URL
           ├─ 5. Write       World + India stories composed only from claims
           │                 (India-angle wins; nothing appears twice)
           ├─ 6. Words       hard words + respelling + plain definition
           └─ 7. Publish     static JSON/HTML → GitHub Pages
                             (on failure: keep yesterday's + banner)

Reader's phone ──tap Follow──> prefilled GitHub issue ──> owner submits
                                                            │
           next run reads open issues (owner-authored only) ─┘
           └─ Gemini + Google Search grounding: research the story from its
              actual beginning; thereafter append one timeline entry per day;
              auto-close after ~14 quiet days
```

One platform. No secrets in the client, because there is no client-side write
path — Follow rides on GitHub's own auth. The cost is that the repo is public
and searchable, which is a step beyond the "unlisted, not private" trade in the
spec; that was accepted knowingly.

**Consequence of the public repo:** anyone can open an issue, so the Follow
workflow must act only on issues authored by the repo owner and ignore the
rest. This is the one security-relevant detail in the whole design.

---

## 9. Sources

- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) · [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search) · [structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Groq rate limits](https://console.groq.com/docs/rate-limits)
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) · [KV limits](https://developers.cloudflare.com/kv/platform/limits/) · [Pages limits](https://developers.cloudflare.com/pages/platform/limits/) · [static assets billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/) · [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [GitHub Actions billing](https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions) · [events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) · [GitHub plans](https://docs.github.com/get-started/learning-about-github/githubs-products)
- [NewsAPI pricing](https://newsapi.org/pricing) · [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) · [Jina Reader](https://jina.ai/reader/)
- [Faithful by Construction (arXiv 2606.23989)](https://arxiv.org/pdf/2606.23989) · [NTS-CoT (arXiv 2606.13171)](https://arxiv.org/pdf/2606.13171)
- [Cloudflare Pages→Workers migration guidance](https://developers.cloudflare.com/workers/static-assets/migration-guides/migrate-from-pages/) · [Speech synthesis in Safari](https://weboutloud.io/bulletin/speech_synthesis_in_safari/)
