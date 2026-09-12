"""Suite-wide test setup.

`config.py` calls `load_dotenv()` at import time, and the live `.env` on the
server has `DRY_RUN=false`. Tests must not depend on the host's `.env` — nor on
which test module happens to import `iblu_keeper` first — so mock mode is
pinned here, in the one file pytest always imports before collecting anything.

Tests that need live behaviour substitute a stub `settings` object on the
module under test (`Settings` is a frozen dataclass); see `live_settings`.
"""

from __future__ import annotations

import os

os.environ["DRY_RUN"] = "true"

import pytest  # noqa: E402  (must follow the env pin above)


class _LiveSettings:
    """Minimal stand-in for `settings` with mock mode switched off."""

    use_mock = False
    dry_run = False

    def __init__(self, **overrides):
        for key, value in overrides.items():
            setattr(self, key, value)


@pytest.fixture
def live_settings():
    """Factory for a live-mode settings stub (see module docstring)."""
    return _LiveSettings
