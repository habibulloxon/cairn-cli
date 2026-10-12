"""User options in ~/.cairn/config.json (`cairn config KEY VALUE`)."""

import json

from cairn import paths
from cairn.util import load_json

DEFAULTS = {
    "ticket_url": "",   # e.g. https://linear.app/acme/issue/{ticket}
    "nudge": True,      # remind at session start when a session for this work exists
    "nudge_hours": 24,  # ...counting sessions active within this many hours (same branch: any age)
}


def load():
    cfg = dict(DEFAULTS)
    stored = load_json(paths.CONFIG_PATH, {})
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return cfg


def set_value(key, raw):
    """Store `raw` under `key`, parsed as JSON when it is (true, 24, "x"), else as text."""
    if key not in DEFAULTS:
        raise KeyError(key)
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    stored = load_json(paths.CONFIG_PATH, {})
    stored = stored if isinstance(stored, dict) else {}
    stored[key] = value
    paths.DATA.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_PATH.write_text(json.dumps(stored, indent=2) + "\n")
    return value
