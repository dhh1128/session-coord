"""Fixtures for the session-coord suite.

Run with: uv run --no-project --with pytest python -m pytest tests -q
(or `python3 -m pytest tests -q` with pytest installed). Python 3.9 or later.


The sys.path insert makes the package importable under a bare `pytest tests` from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_tmux(monkeypatch):
    """`cli._live` reads the REAL tmux server unless replaced, so every test replaces it.

    Without this a test's labels would depend on which panes happen to exist on the box that
    runs it. None means tmux could not be asked, so stored windows stand; an empty table would
    mean no panes exist, which makes every record stale. A test about live windows sets its own.
    """
    from sessioncoord import cli
    monkeypatch.setattr(cli, "_live_table", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_hosts(monkeypatch, tmp_path):
    """`msg` reads the hosts file on every send, so the box's real one must not leak in."""
    monkeypatch.setenv("SESSION_COORD_HOSTS", str(tmp_path / "no-hosts"))
