"""The owner's decisions on YouTube channels (config/channel_decisions.yaml): confirmed as a government body's own meeting
channel, or rejected. They win over the crawler's own name checks (transcripts.official_channel), take confirmed channels'
videos out of the Rumor Mill - Video export, and are the labeled examples the open model learns from (channel_ai.py)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

_CACHE: dict[str, tuple[float, dict]] = {}


def _root() -> Path:
    from .config import ROOT

    return Path(ROOT)


def load(root: Path | None = None) -> dict[str, dict]:
    """{channel id: decision}: the owner's (config/channel_decisions.yaml) over the open model's confirmations of channels
    as good as the owner's (state/channel_ai.json "auto", channel_ai.py). Missing or unreadable files mean no decisions."""
    root = Path(root or _root())
    p, sp = root / "config" / "channel_decisions.yaml", root / "state" / "channel_ai.json"
    mts = []
    for f in (p, sp):
        try:
            mts.append(f.stat().st_mtime)
        except OSError:
            mts.append(0)
    hit = _CACHE.get(str(root))
    if hit and hit[0] == tuple(mts):
        return hit[1]
    out: dict[str, dict] = {}
    try:
        auto = (json.loads(sp.read_text(encoding="utf-8")) or {}).get("auto") or {}
    except (OSError, ValueError):
        auto = {}
    try:
        d = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("channels") or {}
    except (OSError, yaml.YAMLError):
        d = {}
    for src in (auto, d):    # the owner's last, so they win
        for k, v in src.items():
            if isinstance(v, dict) and v.get("status") in ("confirmed", "rejected"):
                out[str(k)] = dict(v, id=str(k))
    _CACHE[str(root)] = (tuple(mts), out)
    return out


def channel_id(ch: dict | None) -> str:
    """The UC... id of a channel dict ({"id"} or {"url": .../channel/UC...})."""
    if not ch:
        return ""
    if ch.get("id"):
        return str(ch["id"])
    u = str(ch.get("url") or "").rstrip("/")
    return u.rsplit("/", 1)[-1] if "/channel/" in u else ""


def decision(ch: dict | None, root: Path | None = None) -> dict | None:
    return load(root).get(channel_id(ch))


def basis(d: dict) -> str:
    """Why the channel counts as the body's own (or not), as the records state it."""
    if d.get("status") == "confirmed" and str(d.get("by", "owner")).startswith("open model"):
        return f"confirmed by the open model on {d.get('decided', '')} as {d.get('title', 'the channel')}, as good as the channels the owner confirmed: {d.get('note', '')}"[:300]
    if d.get("status") == "confirmed":
        return f"hand-checked: confirmed by Electo's owner on {d.get('decided', '')} as {d.get('title', 'the channel')}, the body's own meeting channel" \
               + (f" (linked from {d['found_on']})" if d.get("found_on") else "")
    return f"rejected by Electo's owner on {d.get('decided', '')}: not the body's own channel"
