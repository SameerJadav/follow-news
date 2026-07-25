# Follow — Decisions

Settled 2026-07-25, following the research in `research.md`. These are inputs to
the build, not open questions.

## Platform

| | Decision |
|---|---|
| Repo | **Public** GitHub repo |
| Hosting | **GitHub Pages** (static) |
| Compute | GitHub Actions — unlimited minutes on a public repo |
| LLM | Gemini free tier, with Google Search grounding |
| Reader proxy | `r.jina.ai` keyless (20 RPM) — no API key |
| Phone | Android |

Accepted trade: the repo is public and therefore **searchable**, not merely
unlisted. Digest history, prompts, and the followed-story list are all
discoverable by anyone who looks. This goes further than the "unlisted, not
private" trade in `product.md` §Constraint: free only, and was chosen knowingly.

Accepted trade: free-tier Gemini content is used to improve Google's products.

## Schedule

- Digest must be **ready by 07:00 IST**.
- Pipeline runs **~02:00 IST** with retry crons — ~5 hours of slack against
  GitHub Actions' documented delays and dropped runs.
- Job must be **idempotent**; a duplicate or retried run is harmless.

## Editorial

**In scope:** politics, conflict, economy, disasters; major sport (national
moments only — a World Cup final, not a league fixture); science, health,
climate, technology.

**Out of scope:** culture, entertainment, obituaries.

- **Sections:** India-angle wins. Any story with a significant India dimension
  goes in India; World is genuinely elsewhere-only. **No story appears twice.**
- **Length:** variable by weight — lead story ~500 words, secondary ~200.
- **Reading level:** plain adult English. Clear and jargon-free, but *not*
  deliberately simplified — closer to a good explainer site than a children's
  news service.
- **Story count:** not fixed. No padding, no quota stories.

## Sourcing and trust

- **Broad pool, quality-weighted.** Keep every working feed, weight claims by
  outlet reliability, and require corroboration before a weak-source claim
  enters a story.
- **Claim-anchored generation** (per `research.md` §6): extract atomic claims
  and anchor each to one source URL *before* writing. Stories are composed only
  from anchored claims. No post-hoc attribution.
- **Thin sourcing:** shown as a **badge at the top of the story**.
- Numbers are never averaged or blended across sources. Two sources disagreeing
  produces two claims, not one figure.

## Follow

- **Registration:** tap Follow → opens a **prefilled GitHub issue** → submit.
  No secrets in the client, no second platform.
  - **Security requirement:** the repo is public, so anyone can open an issue.
    The workflow must **only act on issues authored by the repo owner** and
    ignore everything else.
- **Backstory:** built with Gemini + Google Search grounding, researched from
  the story's actual beginning.
- **Search Suggestions chips are displayed**, as the grounding Terms require.
- **Closure:** auto-close after ~14 days with no significant development, with
  a final entry. **No cap** on active follows; quota is protected by batching.

## Resilience

- **On failure or delay:** serve **yesterday's digest with an honest banner**
  ("Today's isn't ready yet — last updated 6am"), replaced automatically when
  the run lands. Never a blank screen, never a half-built digest presented as
  complete.
- **Degrade, don't fail:** a digest built from 6 of 14 sources still ships.
- Feed health checks must detect **HTTP 200 with zero items** — two feeds
  already behave this way today (The Wire, The Print).
- LLM calls batched to **~10–30 requests per morning total**, never one per
  article, so the pipeline is indifferent to whether the free tier is 250 or
  1,500 RPD.
- On 429: wait for the window to lift and resume. Never fail the morning.

## Archive

- **Full accessible archive**, browsable as a **date list, newest first**.
- No search index (revisit only if finding old stories proves hard in practice).
- The daily digest still has a hard close; the archive is a separate,
  unpromoted surface.

## Words to Know

- LLM emits a **phonetic respelling** (`SANK-shunz`), not IPA — directly usable
  by a non-native reader without knowing IPA.
- Tap-to-hear via the browser's `speechSynthesis`; free, offline, on-device.
  Android gives reliable voice selection.
- `dictionaryapi.dev` is **not** a dependency — 75% IPA coverage, 33% audio,
  and it fails on inflected forms (`sanctions`).

## Secrets

`GEMINI_API_KEY` lives in GitHub Actions secrets. Because the repo is public:
never echo it, and do not use `pull_request_target` anywhere in the workflow.

## Noted gap

Excluding obituaries means the death of a globally significant figure will not
appear. That is a real "everyone is talking about this" event the digest will
miss. Flagged, not overridden — revisit if it bites.
