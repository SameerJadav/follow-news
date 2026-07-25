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


def test_is_rate_limited_recognises_string_fallback():
    """Not every exception this wraps carries a .code (e.g. the SDK could
    raise a bare Exception with 429 only in its message) -- classification
    must not depend solely on the ClientError shape."""
    assert ratelimit.is_rate_limited(Exception("429 Client Error: Too Many Requests"))
    assert not ratelimit.is_rate_limited(Exception("500 Internal Server Error"))
