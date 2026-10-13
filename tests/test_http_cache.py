"""Regression tests for the HTTP cache's handling of a missing resource.

The negative cache is the difference between a build that asks the SEC about
a nonexistent resource once and one that asks about it on every run. Getting
it wrong is invisible locally and looks like abuse from the far end.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_http_")

from finlake import http_client  # noqa: E402


def test_a_cached_404_is_not_read_back_as_a_cache_miss():
    """The bug: a 404 was stored as JSON `null`, and `_read_cache` returned
    None for it — the same value it returns for "nothing cached". `get_json`
    read that as a miss and re-requested the URL every single time, so the
    negative cache never suppressed a single request despite the comment
    saying it did.
    """
    url = "https://example.invalid/definitely-missing.json"
    path = http_client._cache_path(url)
    http_client._write_cache(path, None)          # what a 404 records

    cached = http_client._read_cache(path, max_age_days=None)
    assert cached is http_client._MISS, (
        "a recorded 404 read back as a cache miss, so it would be refetched "
        "on every call")
    assert cached is not None


def test_a_cached_empty_payload_is_still_a_hit():
    """An empty list/dict is a real response, not a miss. It must not be
    confused with a 404 — otherwise a legitimately empty resource gets
    refetched forever, which is the same bug wearing a different hat."""
    for payload in ([], {}, 0, ""):
        url = f"https://example.invalid/empty-{type(payload).__name__}-{payload!r}.json"
        path = http_client._cache_path(url)
        http_client._write_cache(path, payload)
        cached = http_client._read_cache(path, max_age_days=None)
        assert cached is not http_client._MISS, (
            f"empty payload {payload!r} was mistaken for a 404")
        assert cached == payload


def test_cache_paths_are_unique_per_url():
    """Two URLs sharing a 60-character prefix must not share a cache file.
    The slug is truncated, so the hash suffix is what keeps them apart —
    without it, one company's facts would be served for another's."""
    base = "https://data.sec.gov/api/xbrl/companyfacts/CIK00000000"
    a = http_client._cache_path(f"{base}01.json")
    b = http_client._cache_path(f"{base}02.json")
    assert a != b, "distinct URLs collided onto one cache file"
