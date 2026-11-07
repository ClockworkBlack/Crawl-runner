from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("GACRAWL_ROOT", Path(__file__).resolve().parent.parent))


def load(path: Path | None = None) -> dict:
    cfg_dir = ROOT / "config"
    settings = yaml.load((path or cfg_dir / "settings.yaml").read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    # config/private/settings.yaml (kept only in the private repository; the public runner fetches it at run time):
    # the requester's signature and anything else not to publish. Merged over settings.yaml, one level deep.
    priv = cfg_dir / "private" / "settings.yaml"
    if path is None and priv.exists():
        extra = yaml.safe_load(priv.read_text(encoding="utf-8")) or {}
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(settings.get(k), dict):
                settings[k] = {**settings[k], **v}
            else:
                settings[k] = v
    settings.setdefault("data_dir", "data")
    settings.setdefault("state_dir", "state")
    settings["data_dir"] = ROOT / settings["data_dir"]
    settings["state_dir"] = ROOT / settings["state_dir"]
    if os.environ.get("GACRAWL_DB"):
        settings["db_path"] = Path(os.environ["GACRAWL_DB"])
    return settings


def make_store(cfg: dict):
    from .store import Store

    return Store(cfg.get("db_path") or cfg["data_dir"])


def make_fetcher(cfg: dict):
    from .fetch import Fetcher

    http = cfg.get("http", {})
    return Fetcher(
        user_agent=_user_agent(http),
        state_dir=cfg["state_dir"],
        min_interval=float(http.get("min_interval_seconds", 2.0)),
        timeout=float(http.get("timeout_seconds", 30)),
        max_bytes=int(http.get("max_bytes", 25_000_000)),
        max_doc_bytes=int(http.get("max_doc_bytes", 150_000_000)),
        blocked_ttl_days=int(http.get("blocked_ttl_days", 7)),
        host_intervals=http.get("host_intervals") or {},
        api_endpoints=http.get("api_endpoints") or [],
        host_timeouts=http.get("host_timeouts") or {},
        after_answer=http.get("space_after_answer") or [],
    )


def _user_agent(http: dict) -> str:
    """Honest User-Agent; adds a contact address when one is configured (Wikimedia asks automated clients for one).
    GACRAWL_CONTACT, else the open-records project mailbox (ORR_FROM). Never a personal address."""
    ua = os.environ.get("GACRAWL_USER_AGENT") or http.get("user_agent", "GACivicCrawler/0.3")
    contact = os.environ.get("GACRAWL_CONTACT") or os.environ.get("ORR_FROM") or http.get("contact")
    if contact and contact not in ua:
        ua = ua[:-1] + f"; {contact})" if ua.endswith(")") else f"{ua} ({contact})"
    return ua
