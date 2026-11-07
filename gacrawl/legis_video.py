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


VIMEO_API = "https://api.vimeo.com"


def _vimeo_headers() -> dict:
    import os

    tok = os.environ.get("VIMEO_TOKEN", "").strip()
    return {"Authorization": f"bearer {tok}", "Accept": "application/vnd.vimeo.*+json;version=3.4"} if tok else {}


def vimeo_api_videos(fetcher, url: str, since: str, deadline=None, max_pages: int = 60) -> list[dict]:
    """A Vimeo account's public videos through Vimeo's own API (needs the VIMEO_TOKEN secret: an unauthenticated
    app token, public data only)."""
    user = url.rstrip("/").split("vimeo.com/")[-1].split("/")[0]
    h = _vimeo_headers()
    out, nxt, pages = [], f"/users/{user}/videos?per_page=100&sort=date&direction=desc&fields=uri,name,link,created_time,release_time,duration", 0
    while nxt and pages < max_pages and not (deadline and time.monotonic() > deadline):
        r = fetcher.get(VIMEO_API + nxt, headers=h, use_cache=False)
        if r.status != 200:
            break
        d = r.json()
        pages += 1
        stop = False
        for v in d.get("data") or []:
            day = _d10(v.get("release_time") or v.get("created_time"))
            if day and day < since:
                stop = True
                break
            out.append({"url": v.get("link") or "", "title": v.get("name") or "", "date": day, "platform": "vimeo",
                        "video_id": (v.get("uri") or "").rsplit("/", 1)[-1], "duration": v.get("duration")})
        nxt = None if stop else ((d.get("paging") or {}).get("next"))
    return out


def channel_videos(fetcher, ch: dict, since: str, deadline=None, log=print) -> list[dict]:
    from .transcripts import youtube_videos

    url = ch.get("url") or ""
    if "vimeo.com" in url:
        if _vimeo_headers():
            vs = vimeo_api_videos(fetcher, url, since, deadline)
            if vs:
                return vs
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
        from .channels import BROWSER_LOCK

        with BROWSER_LOCK, sync_playwright() as p:
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


def link_videos(fetcher, store, root: Path, deadline=None, log=print, pages: bool = True) -> dict:
    from .registry import load_yaml

    cfg = (load_yaml(root / "config" / "legislature.yaml").get("video") or {})
    chans = [dict(c) for c in cfg.get("channels") or []]
    # the legislature's own pages are read only when asked (inside the off-peak window); the channels' APIs any time
    for c in (discover_channels(fetcher, cfg.get("official_pages") or [], log) if pages else []):
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
                                           "video_id": v.get("video_id") or (v["url"].rstrip("/").rsplit("/", 1)[-1] if v["platform"] == "vimeo" else ""),
                                           "opened": True})
                        linked += store.add(rec)
    store.flush()
    return {"channels": len(chans), "videos": len(videos), "links": linked}


# ----------------------------------------------------------------------------- what was said about the bill
HEARING_SOURCE = "ga:legis-hearing"


def _vimeo_captions(fetcher, vid: str) -> list[tuple[float, str]]:
    from .transcripts import parse_vtt

    h = _vimeo_headers()
    r = fetcher.get(f"{VIMEO_API}/videos/{vid}/texttracks", headers=h, use_cache=False)
    if r.status != 200:
        return []
    j = r.json()
    tracks = [t for t in ((j.get("data") if isinstance(j, dict) else j) or []) if isinstance(t, dict) and t.get("link")]
    tracks.sort(key=lambda t: (not str(t.get("language") or "").startswith("en"), not t.get("active")))
    for t in tracks[:2]:
        v = fetcher.get(t["link"], use_cache=False)
        if v.status == 200:
            cues = parse_vtt(v.text)
            if cues:
                return cues
    return []


def _vimeo_download(fetcher, vid: str) -> str:
    """The smallest file the account itself offers for download (only when the owner allows downloads)."""
    r = fetcher.get(f"{VIMEO_API}/videos/{vid}?fields=download", headers=_vimeo_headers(), use_cache=False)
    if r.status != 200:
        return ""
    j = r.json()
    dl = [d for d in ((j.get("download") if isinstance(j, dict) else j) or []) if isinstance(d, dict) and d.get("link")
          and "video" in str(d.get("type") or "video")]
    dl.sort(key=lambda d: d.get("size") or 1e18)
    return dl[0]["link"] if dl else ""


def bill_moments(cues: list[tuple[float, str]], number: str, after: int = 10) -> list[dict]:
    """Where the bill comes up in a hearing: each mention of its number (in any spoken or written form) with the
    next stretch of talk (about a minute), time-coded."""
    from .bill_reasons import bill_rx, excerpts_for
    from .transcripts import hms

    rx = bill_rx(number)
    spoken = re.compile(rx.pattern.replace(r"\s*", r"[\s,-]*"), re.I)
    out, last = [], -1e9
    for i, (t, txt) in enumerate(cues):
        if not (rx.search(txt) or spoken.search(txt)) or t - last < 90:
            continue
        last = t
        window = " ".join(x for _, x in cues[i:i + after])
        ex = excerpts_for(window, number, limit=2, width=600) or [{"x": window[:600], "stance": "context"}]
        out.append({"t": round(t, 1), "at": hms(t), "x": " ".join(e["x"] for e in ex)[:900], "stance": ex[0].get("stance", "context")})
    return out[:6]


def hearings(fetcher, store, cfg: dict, deadline=None, log=print) -> dict:
    """Transcripts of the hearings linked to bills: Vimeo's own caption files first, else (when the House allows
    downloads) the downloaded file transcribed with faster-whisper on the runner. Then every linked bill gets the
    moments where it is discussed (kind bill_reason, source ga:legis-hearing, evidence primary)."""
    from .transcripts import cues_to_record, transcribe_media

    if not _vimeo_headers():
        return {"skipped": "no VIMEO_TOKEN"}
    tc = cfg.get("transcribe") or {}
    by_video: dict[str, list[dict]] = {}
    for r in store.iter_query("kind='bill_video' AND source_id=?", (SOURCE,)):
        m = r["meta"]
        if m.get("platform") == "vimeo" and m.get("video_id"):
            by_video.setdefault(m["video_id"], []).append({**m, "url": r["url"], "title": r.get("title") or ""})
    todo = [(vid, links) for vid, links in by_video.items() if not store.kv_get(f"hearing_tx:{vid}")]
    todo.sort(key=lambda kv: max(x.get("video_date") or "" for x in kv[1]), reverse=True)
    out = {"videos": len(by_video), "waiting": len(todo), "captions": 0, "transcribed": 0, "no_text": 0, "moments": 0}
    for vid, links in todo:
        if deadline and time.monotonic() > deadline:
            break
        how, cues = "captions", []
        try:
            cues = _vimeo_captions(fetcher, vid)
            if not cues:
                dl = _vimeo_download(fetcher, vid)
                if dl:
                    how = "faster-whisper " + str(tc.get("model") or "base")
                    left = (deadline - time.monotonic()) if deadline else float(tc.get("budget_seconds") or 3600)
                    cues = transcribe_media(fetcher, dl, max_mb=int(tc.get("hearing_max_mb") or 1500),
                                            model_size=str(tc.get("model") or "base"),
                                            budget_seconds=max(60.0, min(float(tc.get("hearing_budget_seconds") or 5400), left - 60))) or []
        except Exception as exc:  # noqa: BLE001
            log(f"  hearing {vid}: {type(exc).__name__}: {exc}"[:160])
            continue
        if not cues:
            out["no_text"] += 1
            store.kv_set(f"hearing_tx:{vid}", "none:" + dt.date.today().isoformat())
            continue
        out["captions" if how == "captions" else "transcribed"] += 1
        first = links[0]
        rec = cues_to_record(cues, HEARING_SOURCE, first["url"], first["title"],
                             {"video_id": vid, "platform": "vimeo", "committee": first.get("committee"),
                              "date": first.get("video_date"), "how": how})
        if rec:
            rec.published = first.get("video_date") or None
            store.add(rec)
        for ln in links:
            for mo in bill_moments(cues, ln["number"]):
                r = Record(HEARING_SOURCE, "bill_reason", f"{ln['url']}#t={int(mo['t'])}s",
                           title=f"{ln.get('committee') or 'Committee'} hearing, {ln.get('video_date')} (at {mo['at']})",
                           text=mo["x"], published=ln.get("video_date") or None, channel="vimeo", jurisdiction="ga",
                           evidence="primary", key=f"{HEARING_SOURCE}|{ln['legislation_id']}|{vid}|{int(mo['t'])}",
                           meta={"legislation_id": ln["legislation_id"], "number": ln["number"], "session": ln.get("session"),
                                 "outlet": f"{ln.get('committee') or 'Committee'} hearing (transcript, {how})",
                                 "hearing": True, "at": mo["at"], "video_id": vid,
                                 "excerpts": [{"x": mo["x"], "stance": mo["stance"]}], "opened": True})
                out["moments"] += store.add(r)
        store.kv_set(f"hearing_tx:{vid}", how + ":" + dt.date.today().isoformat())
        store.flush()
    store.flush()
    return out
