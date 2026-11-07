"""Committee hearing video for Georgia bills: which official recording shows the committee that heard a bill, on the
day it acted on it.

1. Each decided bill's own history (legis.ga.gov's `legislation/detail/{id}`, the site's public data service, any
   time of day): the dated steps that name a committee ("House Committee Favorably Reported ...") are kept in the
   store (kv bill_history:{id}), read once for a closed session, again when a current bill moves.
2. The legislature's video: config/legislature.yaml `video.channels`, each an official channel
     - linked from the legislature's own pages (`video.official_pages`: read with the browser, any youtube.com or
       vimeo.com channel link on them is taken), or
     - checked by hand and recorded there with the basis (the same rule as config/overrides.yaml for councils).
   YouTube is listed through the Data API (YOUTUBE_API_KEY); Vimeo through the channel's own feed, found on its
   page (<link rel="alternate" ... rss>), and the page's own "next" links. Robots and the crawler's delays apply.
3. A video whose title names the committee and whose date (in the title, else its upload date) is the day of a
   committee step, or the day before it, is linked to that bill as kind `bill_video` (evidence primary: the
   official recording), with the step it matches. Nothing is transcribed: the link says where the hearing is.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

from .store import Record

SOURCE = "ga:legis-video"
LEGIS = "ga:legis"
TITLE_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\b")
STOP = {"house", "senate", "committee", "on", "the", "and", "of", "&", "subcommittee", "joint", "study", "special"}
HISTORY_RECHECK_DAYS = 7


def _d10(s) -> str:
    return (str(s) if s else "")[:10]


def _feed_date(s) -> str:
    if not s:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(s).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def title_date(title: str) -> str:
    m = TITLE_DATE.search(title or "")
    if not m:
        return ""
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return ""


def committee_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in STOP and len(w) > 2}


def committee_steps(detail: dict) -> list[dict]:
    """The dated steps in a bill's detail that name a committee. The data service's field names are not documented,
    so any list of objects with a date and a text that mentions a committee is read."""
    out = []

    def walk(x):
        if isinstance(x, list):
            for it in x:
                if isinstance(it, dict):
                    date = next((_d10(v) for k, v in it.items() if "date" in k.lower() and v), "")
                    text = " ".join(str(v) for k, v in it.items() if isinstance(v, str) and "date" not in k.lower())
                    if date and re.search(r"committee", text, re.I):
                        out.append({"date": date, "step": re.sub(r"\s+", " ", text).strip()[:200]})
                    else:
                        walk(list(it.values()))
                else:
                    walk(it)
        elif isinstance(x, dict):
            walk(list(x.values()))

    walk(detail)
    seen, uniq = set(), []
    for s in sorted(out, key=lambda s: s["date"]):
        k = (s["date"], s["step"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq


def histories(fetcher, store, deadline=None, log=print, limit: int | None = None) -> dict:
    """Committee steps for decided and moving bills (newest sessions first)."""
    from .bill_reasons import substantive
    from .sources.legis import Legis, TokenRefused

    api = Legis(fetcher, log)
    todo = []
    for b in store.iter_query("source_id=? AND kind='bill'", (LEGIS,)):
        m = b["meta"]
        if not substantive(m) or not (m.get("house_committee") or m.get("senate_committee")):
            continue
        mark = store.kv_get(f"bill_history_at:{m['legislation_id']}")
        if mark and ((m.get("status_date") or "") <= mark or
                     (dt.date.today() - dt.date.fromisoformat(mark[:10])).days < HISTORY_RECHECK_DAYS):
            continue
        todo.append(m)
    todo.sort(key=lambda m: (-(int(m.get("session") or 0)), m.get("number") or ""))
    done = 0
    for m in todo:
        if (deadline and time.monotonic() > deadline) or (limit and done >= limit):
            break
        try:
            det = api.call(f"legislation/detail/{m['legislation_id']}")
        except TokenRefused:
            log("  bill histories: legis.ga.gov gave no token; trying next run")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"  bill history {m['number']}: {type(exc).__name__}"[:120])
            continue
        steps = committee_steps(det or {})
        store.kv_set(f"bill_history:{m['legislation_id']}", json.dumps(steps))
        store.kv_set(f"bill_history_at:{m['legislation_id']}", dt.date.today().isoformat())
        done += 1
    store.flush()
    return {"bills_read": done, "waiting": max(0, len(todo) - done)}


def _vimeo_videos(fetcher, url: str, since: str, max_pages: int = 40, deadline=None) -> list[dict]:
    """A Vimeo channel's videos from its own feed (linked on its page) and its own next-page links."""
    from .feeds import parse_feed

    out, page_url, pages = [], url.rstrip("/") + "/videos" if not url.rstrip("/").endswith("/videos") else url, 0
    first = True
    while page_url and pages < max_pages and not (deadline and time.monotonic() > deadline):
        r = fetcher.get(page_url)
        if r.status != 200:
            break
        html = r.text
        pages += 1
        if first:
            first = False
            feed = re.search(r'<link[^>]+type="application/rss\+xml"[^>]+href="([^"]+)"', html) or \
                re.search(r'<link[^>]+href="([^"]+)"[^>]+type="application/rss\+xml"', html)
            if feed:
                fu = feed.group(1) if feed.group(1).startswith("http") else "https://vimeo.com" + feed.group(1)
                fr = fetcher.get(fu)
                if fr.status == 200:
                    for it in parse_feed(fr.content):
                        out.append({"url": it.get("link") or it.get("url") or "", "title": it.get("title") or "",
                                    "date": _feed_date(it.get("published")), "platform": "vimeo"})
        for vid, title in re.findall(r'href="/(\d{6,12})"[^>]*title="([^"]+)"', html):
            out.append({"url": f"https://vimeo.com/{vid}", "title": title, "date": "", "platform": "vimeo"})
        nxt = re.search(r'<a[^>]+rel="next"[^>]+href="([^"]+)"', html) or re.search(r'<link[^>]+rel="next"[^>]+href="([^"]+)"', html)
        page_url = (nxt.group(1) if nxt.group(1).startswith("http") else "https://vimeo.com" + nxt.group(1)) if nxt else None
    seen, uniq = set(), []
    for v in out:
        if v["url"] and v["url"] not in seen and (not v["date"] or v["date"] >= since):
            seen.add(v["url"])
            uniq.append(v)
    return uniq


def channel_videos(fetcher, ch: dict, since: str, deadline=None, log=print) -> list[dict]:
    from .transcripts import youtube_videos

    url = ch.get("url") or ""
    if "vimeo.com" in url:
        return _vimeo_videos(fetcher, url, since, deadline=deadline)
    if "youtube.com" in url:
        import os

        if not os.environ.get("YOUTUBE_API_KEY"):
            log(f"  {ch.get('name')}: no YOUTUBE_API_KEY, YouTube channel not listed")
            return []
        return [{"url": v["url"], "title": v.get("title", ""), "date": _d10(v.get("published") or v.get("date")),
                 "platform": "youtube"} for v in youtube_videos(fetcher, url, since, limit=2000)]
    return []


def _rendered(fetcher, url: str) -> str:
    """The page as the browser shows it (the legislature's pages build their links with scripts). Robots, the
    crawler's delay and a block check apply, as for every page the crawler reads."""
    try:
        from playwright.sync_api import sync_playwright

        from .blocked import looks_blocked
        from .fetch import launch_browser
    except Exception:  # noqa: BLE001
        return ""
    if not fetcher.allowed(url):
        return ""
    fetcher._check_time()
    fetcher._turn(url, fetcher.robots.crawl_delay(url))
    try:
        with sync_playwright() as p:
            browser = launch_browser(p)
            page = browser.new_page(user_agent=fetcher.user_agent)
            resp = page.goto(url, wait_until="load", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(6000)
            html = page.content()
            # links inside frames too (the Senate press office shows its videos in an embedded frame)
            for fr in page.frames[1:]:
                try:
                    html += fr.content()
                except Exception:  # noqa: BLE001
                    pass
            browser.close()
            if looks_blocked(resp.status if resp else 0, html.encode("utf-8", "ignore")):
                return ""
            return html
    except Exception:  # noqa: BLE001
        return ""


def discover_channels(fetcher, pages: list[str], log=print) -> list[dict]:
    """youtube.com / vimeo.com channel links shown on the legislature's own pages (rendered with the browser when the
    page builds its links with scripts)."""
    found = []
    for p in pages:
        html = ""
        try:
            r = fetcher.get(p)
            html = r.text if r.status == 200 else ""
            if "youtube.com" not in html and "vimeo.com" not in html:
                html = _rendered(fetcher, p) or html
        except Exception as exc:  # noqa: BLE001
            log(f"  video page {p}: {type(exc).__name__}")
            continue
        for u in re.findall(r'https?://(?:www\.)?(?:youtube\.com/(?:@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)|vimeo\.com/[a-z][\w-]+)', html):
            if not re.search(r"vimeo\.com/(?:\d+|video|channels/staffpicks|categories|upload|join|log_in)", u):
                found.append({"url": u.split("?")[0], "basis": f"linked from {p}"})
    seen, out = set(), []
    for c in found:
        if c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    return out


def link_videos(fetcher, store, root: Path, deadline=None, log=print) -> dict:
    from .registry import load_yaml

    cfg = (load_yaml(root / "config" / "legislature.yaml").get("video") or {})
    chans = [dict(c) for c in cfg.get("channels") or []]
    for c in discover_channels(fetcher, cfg.get("official_pages") or [], log):
        if c["url"] not in {x.get("url") for x in chans}:
            chans.append({"name": c["url"], **c})
    since = str(cfg.get("since") or "2019-01-01")
    videos = []
    for ch in chans:
        if deadline and time.monotonic() > deadline:
            break
        try:
            vs = channel_videos(fetcher, ch, since, deadline, log)
        except Exception as exc:  # noqa: BLE001
            log(f"  {ch.get('name')}: {type(exc).__name__}: {exc}"[:160])
            continue
        for v in vs:
            v["channel"] = ch.get("name") or ch.get("url")
            v["day"] = title_date(v["title"]) or v["date"]
            v["words"] = committee_words(v["title"])
        videos += [v for v in vs if v["day"]]
        log(f"  {ch.get('name') or ch.get('url')}: {len(vs)} videos")
    by_day: dict[str, list[dict]] = {}
    for v in videos:
        by_day.setdefault(v["day"], []).append(v)
    linked = 0
    for b in store.iter_query("source_id=? AND kind='bill'", (LEGIS,)):
        m = b["meta"]
        raw = store.kv_get(f"bill_history:{m['legislation_id']}")
        if not raw:
            continue
        steps = json.loads(raw)
        for s in steps:
            chamber_c = m.get("senate_committee") if re.search(r"\bsenate\b", s["step"], re.I) else m.get("house_committee")
            want = committee_words(chamber_c or "")
            if not want:
                continue
            d = dt.date.fromisoformat(s["date"])
            for day in (d.isoformat(), (d - dt.timedelta(days=1)).isoformat()):
                for v in by_day.get(day, []):
                    if len(want & v["words"]) >= max(1, min(2, len(want))):
                        rec = Record(SOURCE, "bill_video", v["url"], title=v["title"][:200],
                                     text=f"{m['number']}: {s['step']} ({s['date']}). {chamber_c} — {v['title']}",
                                     published=v["day"], channel=v["platform"], jurisdiction="ga", evidence="primary",
                                     key=f"{SOURCE}|{m['legislation_id']}|{v['url']}",
                                     meta={"legislation_id": m["legislation_id"], "number": m["number"], "session": m.get("session"),
                                           "committee": chamber_c, "step": s["step"], "step_date": s["date"],
                                           "video_date": v["day"], "platform": v["platform"], "channel": v["channel"],
                                           "opened": True})
                        linked += store.add(rec)
    store.flush()
    return {"channels": len(chans), "videos": len(videos), "links": linked}
