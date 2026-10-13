"""Test isolation. This file exists to stop the suite destroying your cache.

WHAT HAPPENED. Most test modules open with

    os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(...)
    from finlake import ...

which looks airtight and is not. `finlake.config` resolves `DATA_DIR` ONCE, at
import time. Whichever module imports finlake first wins, and every later
module's assignment is a no-op — it sets the variable, `config.DATA_DIR` keeps
whatever it already had, and the module's `DELETE FROM facts` runs wherever
that first import pointed.

Which was the real cache. `smoke_test.py` sits at the repo root, matches
pytest's default `*_test.py` collection glob, and imports finlake at module
level with no `FINLAKE_HOME` set. It defines no test functions so it never
appears in the collected list — but pytest still imports it, before anything
in `tests/`, and that import bound `DATA_DIR` to `~/.finlake`.

Running the suite then deleted, from the production cache: 15.5 million facts,
every securities and ticker_map row, 188,000 macro observations, 67,000 news
articles, and the market snapshot tables. Nothing failed. All 133 tests passed.

Two guards, because the first one alone is what everybody already thought they
had:

  1. `conftest.py` is imported by pytest before ANY collection, so setting the
     environment here happens before any import of finlake can resolve a path.
  2. A hard assertion that the resolved path is not the real cache, so if
     something ever manages to import finlake before this file, the run stops
     instead of quietly deleting.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Set BEFORE finlake is importable-with-effect. Not `setdefault`: a stale
# FINLAKE_HOME exported in the shell would otherwise be honoured, and pointing
# the suite at a real cache is exactly what must never happen.
_TEST_HOME = tempfile.mkdtemp(prefix="finlake_pytest_")
os.environ["FINLAKE_HOME"] = _TEST_HOME

# Belt and braces: the resolved path must be the temp one. This import is the
# first thing that touches config, so it is also the thing that fixes the
# path for the rest of the session.
from finlake import config as _config  # noqa: E402

_REAL_HOME = Path.home() / ".finlake"

if Path(_config.DATA_DIR).resolve() == _REAL_HOME.resolve():
    raise RuntimeError(
        f"REFUSING TO RUN: the test suite resolved FINLAKE_HOME to the real "
        f"cache at {_REAL_HOME}. Several test modules delete whole tables, so "
        f"this would destroy it. Something imported finlake before "
        f"conftest.py ran — check for a module matching pytest's collection "
        f"globs (test_*.py, *_test.py) that imports finlake at module level."
    )

_config.ensure_dirs()


def pytest_report_header(config) -> str:
    return f"finlake cache for this run: {_config.DATA_DIR}"
