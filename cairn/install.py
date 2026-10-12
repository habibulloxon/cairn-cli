"""Adding and removing cairn's hooks and status line in ~/.claude/settings.json."""

import json
import os
import shlex
import shutil
import time

from cairn import paths
from cairn.hooks import HOOK_STATUS
from cairn.util import load_json


def cairn_command():
    """How settings.json should run cairn: a path that survives upgrades and moving the checkout."""
    if os.environ.get("CAIRN_BIN"):  # set by the Homebrew wrapper: /opt/homebrew/bin/cairn
        return shlex.quote(os.environ["CAIRN_BIN"])
    return " ".join(shlex.quote(a) for a in paths.self_argv())


def _cairn_args(cmd):
    """The arguments after `cairn` (or legacy `cairn.py`) in a command line, or None if it isn't cairn's."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    for i, part in enumerate(parts):
        if os.path.basename(part) in ("cairn", "cairn.py"):
            return parts[i + 1:]
    return None


def is_cairn_hook(hook):
    args = _cairn_args(hook.get("command", "")) if isinstance(hook, dict) else None
    return args == ["hook"]


def unwrap_statusline(cmd):
    """The status line cairn wrapped ("" if none) when `cmd` is cairn's; None if `cmd` isn't cairn's."""
    args = _cairn_args(cmd or "")
    if not args or args[0] != "statusline":
        return None
    return args[args.index("--wrap") + 1] if "--wrap" in args[:-1] else ""


def _strip_hooks(hooks):
    for event in list(hooks):
        for group in hooks[event]:
            group["hooks"] = [h for h in group.get("hooks", []) if not is_cairn_hook(h)]
        hooks[event] = [g for g in hooks[event] if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]


def _write(settings):
    """Back up settings.json, then replace it atomically. Returns the backup path (or None)."""
    backup = None
    if paths.SETTINGS.exists():
        backup = paths.SETTINGS.with_name(f"settings.json.cairn-backup-{int(time.time())}")
        shutil.copy2(paths.SETTINGS, backup)
    paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.SETTINGS.with_name("settings.json.cairn-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.SETTINGS)
    return backup


def _read():
    if not paths.SETTINGS.exists():
        return {}
    settings = load_json(paths.SETTINGS, None)
    if not isinstance(settings, dict):
        raise ValueError(f"{paths.SETTINGS} isn't valid JSON; fix it first, cairn won't overwrite it.")
    return settings


def install(statusline=True):
    """Add cairn's hooks (replacing older cairn entries) and wrap the status line. Returns the backup path."""
    settings = _read()
    hooks = settings.setdefault("hooks", {})
    _strip_hooks(hooks)
    command = cairn_command()
    for event in HOOK_STATUS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": f"{command} hook", "timeout": 5}]})
    if statusline:
        current = (settings.get("statusLine") or {}).get("command", "")
        original = unwrap_statusline(current)
        wrap = current if original is None else original
        line = f"{command} statusline" + (f" --wrap {shlex.quote(wrap)}" if wrap else "")
        existing = settings.get("statusLine") or {"padding": 0}
        settings["statusLine"] = {**existing, "type": "command", "command": line}
    return _write(settings)


def uninstall():
    """Remove cairn's hooks and restore the status line it wrapped. Returns the backup path."""
    if not paths.SETTINGS.exists():
        return None
    settings = _read()
    _strip_hooks(settings.get("hooks", {}))
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    original = unwrap_statusline((settings.get("statusLine") or {}).get("command", ""))
    if original:
        settings["statusLine"]["command"] = original
    elif original == "":
        settings.pop("statusLine", None)
    return _write(settings)
