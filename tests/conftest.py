"""
Puts the repo root on ``sys.path`` so ``import xiaomi_app`` resolves.

Needed because pytest prepends the test file's *basedir* (``tests/``, the
first directory without an ``__init__.py``) rather than the repo root. A local
``python -m pytest`` happens to work anyway — ``-m`` puts the cwd on the path —
so this only fails in CI, which runs bare ``pytest``. That is exactly how it
got missed once already.

aw-app-template does this with a ``sys.path.insert`` at the top of every test
module. One conftest instead: pytest imports it before collecting anything, so
it covers every test file including the next one somebody adds without
remembering the incantation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
