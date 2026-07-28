"""ratelimit.call_with_resume is the whole 429 wait-and-resume mechanism, so
its edge cases (real failure vs rate limit, server-given retryDelay vs a
backoff guess, a daily quota, and the overall time budget) are exactly the
fragile edges worth a real test rather than trusting the live API."""

from __future__ import annotations

import pytest

import ratelimit


class FakeRateLimitError(Exception):
    """Stands in for google.genai.errors.ClientError so tests don't depend on
    the real SDK's exception shape — only on the two attributes
    ratelimit.py actually reads: .code and .details."""

    def __init__(self, details: dict | None = None, code: int = 429):
        super().__init__(f"{code} RESOURCE_EXHAUSTED")
        self.code = code
        self.details = details or {}


def _fake_clock():
    """A monotonic clock double that advances only when sleep_fn is called,
    so budget tests don't take real wall-clock time."""
    state = {"t": 0.0}

    def sleep_fn(seconds: float) -> None:
        state["t"] += seconds

    def clock() -> float:
        return state["t"]

    return sleep_fn, clock


def test_non_rate_limit_error_raises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("not a rate limit")

    sleep_fn, clock = _fake_clock()
    with pytest.raises(ValueError):
        ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=clock)
    assert calls["n"] == 1
    assert clock() == 0  # sleep_fn was never called


def test_waits_then_resumes():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise FakeRateLimitError({"retryDelay": "1s"})
        return "ok"

    sleep_fn, clock = _fake_clock()
    result = ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=clock)
    assert result == "ok"
    assert attempts["n"] == 3
    assert clock() > 0  # two waits happened


def test_honours_server_retry_delay():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimitError({"quotaId": "x", "retryDelay": "26s"})
        return "ok"

    slept = []

    def sleep_fn(seconds: float) -> None:
        slept.append(seconds)

    ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=lambda: sum(slept))
    assert len(slept) == 1
    assert 26 <= slept[0] <= 29  # base delay plus up to 3s jitter


def test_daily_quota_does_not_wait():
    def fn():
        raise FakeRateLimitError({"quotaId": "GenerateRequestsPerDayPerProject"})

    slept = []
    with pytest.raises(FakeRateLimitError):
        ratelimit.call_with_resume(fn, "test", sleep_fn=slept.append, clock=lambda: 0.0)
    assert slept == []


def test_budget_bounds_total_wait():
    def fn():
        raise FakeRateLimitError({"retryDelay": "500s"})  # forces MAX_SLEEP_S clamping

    sleep_fn, clock = _fake_clock()
    with pytest.raises(FakeRateLimitError):
        ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=clock)
    # every hop clamps to <= MAX_SLEEP_S + jitter, and the loop stops once the
    # next hop would exceed WAIT_BUDGET_S, so total time spent is bounded.
    assert clock() <= ratelimit.WAIT_BUDGET_S + ratelimit.MAX_SLEEP_S + 3


# The verbatim 429 that gemini-3.6-flash + google_search returned on
# 2026-07-28. No quotaId, no quotaValue, no retryDelay, and no "per day" text —
# which is exactly why it was misread as a waitable per-minute limit and
# consumed the whole 45-minute budget retrying a call that could never work.
_OPAQUE_DETAILS = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your "
            "current usage, head to: https://ai.dev/rate-limit. "
        ),
        "status": "RESOURCE_EXHAUSTED",
        "details": [{
            "@type": "type.googleapis.com/google.rpc.Help",
            "links": [{"description": "Learn more about Gemini API quotas",
                       "url": "https://ai.google.dev/gemini-api/docs/rate-limits"}],
        }],
    }
}


def test_the_real_opaque_429_is_classified_as_opaque():
    exc = FakeRateLimitError(_OPAQUE_DETAILS)
    assert ratelimit.is_rate_limited(exc), "it is still a 429"
    assert not ratelimit.is_daily_quota(exc), "it never says 'per day' — this is the trap"
    assert ratelimit.retry_after(exc) is None
    assert ratelimit.daily_limit(exc) is None
    assert ratelimit.quota_facts(exc) == ""
    assert ratelimit.is_opaque_quota(exc)


def test_an_opaque_429_gives_up_without_burning_the_whole_budget():
    """The bug this guards: a capability that does not exist on the model looks
    identical to a per-minute limit, and waiting 45 minutes for it also spends
    all of dossier.MAX_RESEARCH_SECONDS."""
    def fn():
        raise FakeRateLimitError(_OPAQUE_DETAILS)

    sleep_fn, clock = _fake_clock()
    with pytest.raises(FakeRateLimitError):
        ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=clock)

    assert clock() <= ratelimit.OPAQUE_WAIT_BUDGET_S + ratelimit.MAX_SLEEP_S + 3
    assert ratelimit.OPAQUE_WAIT_BUDGET_S < ratelimit.WAIT_BUDGET_S, \
        "an opaque 429 must cost less than a real one"


def test_an_opaque_429_still_gets_a_hop_or_two():
    """Not classified with daily quotas on purpose: a single unexplained 429 on
    the digest's own write pass must not turn a blip into a stale morning."""
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimitError(_OPAQUE_DETAILS)
        return "recovered"

    sleep_fn, clock = _fake_clock()
    assert ratelimit.call_with_resume(fn, "test", sleep_fn=sleep_fn, clock=clock) == "recovered"
    assert clock() > 0, "it did wait before retrying"


def test_a_named_per_minute_429_keeps_the_full_budget():
    """Regression guard on the fix: the shape verified live on 2026-07-25 names
    its quota and carries a retryDelay, so it must NOT be treated as opaque."""
    named = FakeRateLimitError({"error": {"details": [{
        "violations": [{
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "quotaValue": "5"}],
        "retryDelay": "53s"}]}})
    assert not ratelimit.is_opaque_quota(named)

    # retryDelay alone is enough to prove the server means "not right now".
    assert not ratelimit.is_opaque_quota(FakeRateLimitError({"retryDelay": "30s"}))
    # ...and so is a named quota with no delay attached.
    assert not ratelimit.is_opaque_quota(
        FakeRateLimitError({"violations": [{"quotaId": "SomeQuota", "quotaValue": "5"}]})
    )


def test_is_rate_limited_recognises_string_fallback():
    """Not every exception this wraps carries a .code (e.g. the SDK could
    raise a bare Exception with 429 only in its message) -- classification
    must not depend solely on the ClientError shape."""
    assert ratelimit.is_rate_limited(Exception("429 Client Error: Too Many Requests"))
    assert not ratelimit.is_rate_limited(Exception("500 Internal Server Error"))
