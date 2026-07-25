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
5. Read the actual page on the phone.
6. Append an entry below in this shape:

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
