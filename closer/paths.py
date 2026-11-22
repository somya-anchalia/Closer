"""Canonical filesystem locations for the Closer checkout.

Closer is repo-centric: config (closer.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Closer would silently read/write the wrong files.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
