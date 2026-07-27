"""Rate-limit classification and wait-and-resume.

product.md: hitting a free-tier limit "must never break the morning: wait for
the limit to lift and resume, rather than failing." Phases 1-5 shipped a flat
60s sleep for up to 3 attempts (~3 minutes total) with a TODO to fix this in
Phase 6 -- that is not "wait for the limit to lift", it is "give up quickly
and call it retried". This module is the real thing.

Verified live against the actual key (2026-07-25): gemini-3.6-flash's free
tier enforces GenerateRequestsPerMinutePerProjectPerModel-FreeTier = 5 RPM,
and a live 429 carried `"retryDelay": "53s"` in its RetryInfo detail. Honouring
that server-given delay is the correct wait, not a guess -- so retry_after()
is tried first and a guess is only the fallback for a 429 that omits it.

A day-scoped quota (a quotaId containing "PerDay") is a different situation:
waiting inside one job for hours is worse than letting one of the three
staggered --if-missing crons (research.md SS4.1) pick the run back up a couple
of hours later. So a daily quota re-raises immediately rather than sleeping.
"""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

import tracer
from tracer import dbg

T = TypeVar("T")

# 45 minutes of waiting inside one call is well within the ~5h slack between
# the first 02:00 IST cron and a 07:00 IST read, and short enough that a truly
# stuck run still finishes before the next staggered cron would have fired
# anyway. Overridable for local testing (DIGEST_WAIT_BUDGET_S=5 uv run ...).
WAIT_BUDGET_S = float(os.environ.get("DIGEST_WAIT_BUDGET_S", 2700))
MAX_SLEEP_S = 300  # never sleep longer than this in one hop, even on a big backoff
BASE_SLEEP_S = 20  # exponential-backoff base when the server gives no retryDelay
DEFAULT_SLEEP_S = 60  # last-resort sleep if backoff math ever yields something silly

_RETRY_DELAY_RE = re.compile(r"retryDelay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s")
_DAILY_RE = re.compile(r"PerDay|per day", re.IGNORECASE)


def _blob(exc: Exception) -> str:
    """One string covering both the exception's repr and its structured
    `.details` (present on google.genai.errors.ClientError), so classification
    survives an SDK detail-shape change instead of depending on exact
    attribute access."""
    details = getattr(exc, "details", None)
    return f"{exc!r} {details}"


def is_rate_limited(exc: Exception) -> bool:
    """A 429 from the free-tier quota, as opposed to a real failure (bad
    request, network error, auth failure) that must propagate immediately
    rather than being slept on."""
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    blob = _blob(exc)
    return "429" in blob or "RESOURCE_EXHAUSTED" in blob


def retry_after(exc: Exception) -> float | None:
    """The server's own RetryInfo.retryDelay in seconds, when present -- the
    real wait, not a guess. None when the response omitted it."""
    match = _RETRY_DELAY_RE.search(_blob(exc))
    return float(match.group(1)) if match else None


def daily_limit(exc: Exception) -> int | None:
    """The daily ceiling the server just enforced, if it named one.

    Free-tier limits are unpublished, move without notice, and there are two
    different meters on a grounded call — the model's own requests-per-day and
    the Google Search grounding allowance, which are orders of magnitude apart.
    Guessing which one bit is how you end up sized wrong in both directions, so
    this reads the number back off the 429 that actually fired and lets the
    caller remember it instead."""
    blob = _blob(exc)
    for match in re.finditer(
        r'["\']quotaId["\']:\s*["\']([^"\']*)["\'].{0,200}?["\']quotaValue["\']:\s*["\']?(\d+)',
        blob,
        re.DOTALL,
    ):
        if _DAILY_RE.search(match.group(1)):
            return int(match.group(2))
    # quotaValue sometimes precedes quotaId in the violation object.
    for match in re.finditer(
        r'["\']quotaValue["\']:\s*["\']?(\d+)["\']?.{0,200}?["\']quotaId["\']:\s*["\']([^"\']*)["\']',
        blob,
        re.DOTALL,
    ):
        if _DAILY_RE.search(match.group(2)):
            return int(match.group(1))
    return None


def is_daily_quota(exc: Exception) -> bool:
    """True when the violated quotaId names a per-day limit rather than a
    per-minute one -- distinguishes 'wait a minute and resume' from 'this run
    is done, let a later staggered cron pick it up'."""
    return bool(_DAILY_RE.search(_blob(exc)))


def quota_facts(exc: Exception) -> str:
    """Every quotaId/quotaValue pair mentioned in the error, so the *actual*
    free-tier number in force today lands in the Actions log the first time a
    429 is hit -- free-tier limits are unpublished and move without notice
    (research.md SS3.1)."""
    blob = _blob(exc)
    # Both quote styles, because the two places these strings come from differ:
    # a raw HTTP body is JSON (double quotes), but google.genai puts the parsed
    # payload on exc.details as a Python dict, and _blob() repr()s it into
    # single quotes. Matching only JSON meant quota_facts() returned "" on every
    # real 429 -- verified 2026-07-27, the one 429 this project has recorded
    # logged `facts: ''` and taught us nothing about the actual limit.
    ids = re.findall(r'["\']quotaId["\']:\s*["\']([^"\']+)["\']', blob)
    values = re.findall(r'["\']quotaValue["\']:\s*["\']?(\d+)["\']?', blob)
    metrics = re.findall(r'["\']quotaMetric["\']:\s*["\']([^"\']+)["\']', blob)
    facts = list(dict.fromkeys([*ids, *metrics]))
    if not facts and not values:
        return ""
    paired = ", ".join(f"{i}={v}" for i, v in zip(ids or metrics, values)) or ", ".join(facts)
    return f"[{paired}]" if paired else ""


def call_with_resume(
    fn: Callable[[], T],
    label: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call `fn()`; on a rate-limit 429, wait out the server's own retryDelay
    (or a backoff guess) and try again, until either it succeeds, a real
    (non-rate-limit) exception is raised, a daily quota is hit, or the total
    time spent waiting exceeds WAIT_BUDGET_S. This replaces a fixed attempt
    count with a time budget, which is what "wait for the limit to lift and
    resume" actually means for a free tier whose real numbers are unpublished
    and can change without notice.
    """
    start = clock()
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reclassified immediately below
            if not is_rate_limited(exc):
                raise

            if is_daily_quota(exc):
                dbg(
                    f"ratelimit: {label} DAILY QUOTA EXHAUSTED {quota_facts(exc)}; "
                    "not waiting out a day inside one run -- the next staggered "
                    "cron will retry"
                )
                tracer.event("ratelimit", label=label, verdict="daily_quota_exhausted",
                             attempt=attempt + 1, facts=quota_facts(exc))
                raise

            attempt += 1
            elapsed = clock() - start
            wait = retry_after(exc)
            if wait is None:
                wait = BASE_SLEEP_S * (2 ** (attempt - 1))
            wait = min(wait, MAX_SLEEP_S) or DEFAULT_SLEEP_S
            wait += random.uniform(0, 3)  # jitter so staggered crons don't retry in lockstep

            if elapsed + wait > WAIT_BUDGET_S:
                dbg(
                    f"ratelimit: {label} 429, wait budget exhausted "
                    f"(elapsed {elapsed:.0f}s + {wait:.0f}s > {WAIT_BUDGET_S:.0f}s budget) "
                    f"{quota_facts(exc)}"
                )
                tracer.event("ratelimit", label=label, verdict="budget_exhausted",
                             attempt=attempt, elapsed_s=round(elapsed, 1),
                             would_wait_s=round(wait, 1), budget_s=WAIT_BUDGET_S,
                             facts=quota_facts(exc))
                raise

            dbg(
                f"ratelimit: {label} 429, waiting {wait:.0f}s "
                f"(elapsed {elapsed:.0f}s/{WAIT_BUDGET_S:.0f}s) {quota_facts(exc)}"
            )
            # The real free-tier numbers are unpublished and move without
            # notice (research.md §3.1). A week of actual 429s, with the
            # server's own retryDelay, is the only way to know where we sit.
            tracer.event("ratelimit", label=label, verdict="waiting",
                         attempt=attempt, elapsed_s=round(elapsed, 1),
                         wait_s=round(wait, 1), server_retry_delay=retry_after(exc),
                         budget_s=WAIT_BUDGET_S, facts=quota_facts(exc))
            sleep_fn(wait)
