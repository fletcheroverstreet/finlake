"""Polite HTTP.

Two jobs:
  1. Never exceed a source's rate limit (token bucket, thread-safe).
  2. Never fetch the same bytes twice (gzipped on-disk cache keyed by URL).

The cache is what makes the "offline, under a second" requirement achievable:
after the first build, nothing here touches the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import config


class RateLimiter:
    """Token bucket. Blocks until a request is allowed."""

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1, int(rate_per_sec))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self.rate
            )
            self._last = now
            if self._tokens < 1.0:
                sleep_for = (1.0 - self._tokens) / self.rate
                time.sleep(sleep_for)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITER_LOCK = threading.Lock()


def _limiter(host: str, rate: float) -> RateLimiter:
    with _LIMITER_LOCK:
        if host not in _LIMITERS:
            _LIMITERS[host] = RateLimiter(rate)
        return _LIMITERS[host]


def _cache_path(url: str) -> Path:
    """Stable filename for a URL. Prefix keeps the directory browsable."""
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    slug = url.split("//", 1)[-1].replace("/", "_").replace("?", "_")[:60]
    return config.RAW_DIR / f"{slug}__{h}.json.gz"


# A cached 404. Distinct from None, which means "nothing usable in the cache".
#
# Without this distinction the negative cache does not work at all: a 404 is
# stored as JSON `null`, `_read_cache` returns None for it, and `get_json`
# reads None as a cache miss and re-requests the URL on every single call.
# For a build that walks thousands of tickers — many of which legitimately
# have no companyfacts — that turns a one-time miss into a request per ticker
# per run, which is exactly the behaviour the SEC rate-limits for.
_MISS = object()


def _read_cache(path: Path, max_age_days: float | None) -> Any | None:
    if not path.exists():
        return None
    if max_age_days is not None:
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > max_age_days:
            return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None  # corrupt cache entry: treat as a miss
    # A stored `null` is a recorded 404, not an empty hit. Reading the file
    # back as _MISS keeps old cache entries (written before this distinction
    # existed) working without a rebuild.
    return _MISS if payload is None else payload


def _write_cache(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    tmp.replace(path)  # atomic: a killed process never leaves a half-written cache


def get_json(
    url: str,
    *,
    rate: float = 5.0,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    max_age_days: float | None = 1.0,
    max_retries: int = 4,
    allow_404: bool = False,
) -> Any | None:
    """Fetch JSON with caching, rate limiting, and exponential backoff.

    Returns None if the resource 404s and allow_404 is True.
    """
    config.ensure_dirs()

    full_url = url
    if params:
        from urllib.parse import urlencode

        full_url = f"{url}?{urlencode(sorted(params.items()))}"

    path = _cache_path(full_url)
    cached = _read_cache(path, max_age_days)
    if cached is _MISS:
        # A previously recorded 404. Honour it without another request, but
        # still raise for a caller who did not opt into missing resources.
        if allow_404:
            return None
        raise FileNotFoundError(f"404 (cached) {full_url}")
    if cached is not None:
        return cached

    host = full_url.split("/")[2]
    limiter = _limiter(host, rate)

    hdrs = {"Accept-Encoding": "gzip, deflate"}
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(max_retries):
        limiter.acquire()
        try:
            resp = requests.get(full_url, headers=hdrs, timeout=30)
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(2**attempt)
            continue

        if resp.status_code == 404:
            if allow_404:
                _write_cache(path, None)  # cache the miss; don't re-ask forever
                return None
            raise FileNotFoundError(f"404 {full_url}")

        if resp.status_code in (429, 502, 503, 504):
            # Back off hard. 429 from SEC means you've been noticed.
            # Record it: without this, exhausting the retries on a run of 503s
            # raises "Failed to fetch <url>: None", which says nothing about
            # why. Retry-After is not always a number (RFC 7231 also allows an
            # HTTP date), so a bad value falls back to exponential backoff
            # rather than crashing the whole build on a float() parse.
            last_err = RuntimeError(f"HTTP {resp.status_code} after {attempt + 1} tries")
            try:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
            except (TypeError, ValueError):
                wait = float(2**attempt)
            time.sleep(min(wait, 60))
            continue

        try:
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            # A non-retryable status, or a 200 whose body isn't JSON (an
            # interstitial or block page). Retrying a 4xx is pointless, so
            # surface it rather than burning the remaining attempts.
            last_err = exc
            break
        _write_cache(path, payload)
        return payload

    # Network is down but we have a stale copy: better than nothing.
    stale = _read_cache(path, max_age_days=None)
    if stale is _MISS:
        if allow_404:
            return None
    elif stale is not None:
        return stale
    raise RuntimeError(f"Failed to fetch {full_url}: {last_err}")


def sec_get(path_or_url: str, **kwargs: Any) -> Any | None:
    """GET an SEC endpoint with the required User-Agent."""
    url = (
        path_or_url
        if path_or_url.startswith("http")
        else f"{config.SEC_BASE}{path_or_url}"
    )
    headers = {"User-Agent": config.SEC_USER_AGENT, "Host": url.split("/")[2]}
    kwargs.setdefault("rate", config.SEC_RATE_LIMIT)
    kwargs.setdefault("headers", headers)
    return get_json(url, **kwargs)
