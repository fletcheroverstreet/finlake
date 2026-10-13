"""Shared plumbing for the unofficial market provider.

One thing lives here: silencing the provider's own logging for the duration of
a call. It is shared rather than duplicated because the two modules that talk
to that provider — `prices` and `market` — hit the same behaviour, and only
one of them was handling it.

WHY IT IS NEEDED AT ALL. The provider logs a line at ERROR for every symbol
and every endpoint it has no data for, on a logger with no handler of its own,
so Python's last-resort handler prints it straight to stderr. A market sweep
over a universe that still held preferred series produced this, four or five
times per symbol:

    HTTP Error 404: {"quoteSummary":{"result":null,"error":{"code":"Not Found",
    "description":"No fundamentals data found for symbol: ALL-PB"}}}

None of it is actionable and none of it is lost: every outcome is already
recorded per ticker in `fetch_log`, which is queryable, and the tier reports
its own coverage summary. What the raw output did instead was bury the
daemon's status lines under several thousand duplicates of a condition the
code had already handled.

SCOPED AND RESTORED, never disabled globally. A blanket silence would also
hide a provider that has genuinely started refusing everything, which is the
one provider failure that actually matters — and that failure is visible in
the coverage summary rather than in this stream.
"""

from __future__ import annotations

import logging
import warnings
from contextlib import contextmanager

# Every logger the provider and its dependencies write to. `peewee` is here
# because yfinance's optional on-disk cache uses it and narrates its own
# schema migrations at WARNING.
_PROVIDER_LOGGERS = ("yfinance", "yfinance.ticker", "yfinance.data", "peewee")


@contextmanager
def quiet_provider():
    """Silence the provider's per-symbol logging for one call."""
    previous = {}
    for name in _PROVIDER_LOGGERS:
        logger = logging.getLogger(name)
        previous[name] = logger.disabled
        logger.disabled = True
    try:
        with warnings.catch_warnings():
            # The provider warns liberally about deprecated fields on symbols
            # it has partial coverage for. Not actionable per-ticker.
            warnings.simplefilter("ignore")
            yield
    finally:
        for name, was in previous.items():
            logging.getLogger(name).disabled = was
