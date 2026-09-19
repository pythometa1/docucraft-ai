"""The authoring guide, served to signed-in users only.

It lives here rather than in the frontend because everything in the JS bundle
is readable by anyone who loads the login page.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_GUIDE_PATH = Path(__file__).with_name("guide.json")


@lru_cache(maxsize=1)
def load_guide() -> dict:
    return json.loads(_GUIDE_PATH.read_text(encoding="utf-8"))
