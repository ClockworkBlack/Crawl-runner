"""Meeting video transcripts: what a council member said, on the record, searchable by name.

Where transcripts come from (all on official sites or official video platforms, robots permitting)
  Granicus     the meeting video page (MediaPlayer.php?...clip_id=) links its own transcript viewer; that page's
               timestamped text is the transcript. Legistar meetings carry the same video link (EventVideoPath).
  caption files  <track src="...vtt"> on HTML5 players, or links to .vtt / .srt files
  recordings   audio/video files posted on an official site can be transcribed locally with an open-source
               speech model (faster-whisper) when `transcribe.enabled` is on; off by default, time-budgeted
  YouTube      the Data API lists a channel's meeting videos (title, date, description) but does not hand out
               captions, so those are recorded as `video` with transcript_status "read on the video page".
               Only official channels: the government links to it (its own website, or a hand check in
               config/overrides.yaml) and the channel is plainly the government's (official_channel). Videos from
               any other channel go to the Rumor Mill - Video bucket (rumor_video.py), never into the records.

Records: kind `transcript` — text is the whole transcript; meta.segments is [[seconds, char_offset], ...] so a
mention can be cited with its time code. Nothing is built from a URL pattern: transcript links are taken from
the video page itself.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import extract
from .blocked import Blocked
from .fetch import Disallowed
from .store import Record

TIME_RX = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
VTT_CUE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})")
TAG_RX = re.compile(r"<[^>]+>")
TRANSCRIPT_LINK = re.compile(r"TranscriptViewer\.php[^\"'\s<>]*|[^\"'\s<>]+\.(?:vtt|srt)(?:\?[^\"'\s<>]*)?|[^\"'\s<>]*(?:caption|transcript)s?[^\"'\s<>]*\.(?:vtt|srt|txt)", re.I)
VIDEO_PAGE = re.compile(r"granicus\.com/(?:MediaPlayer|player/clip)|clip_id=|/MediaPlayer\.php", re.I)
MEDIA_EXT = re.compile(r"\.(mp3|m4a|wav|mp4|m4v|webm|ogg)(?:$|[?#])", re.I)
MAX_MARKERS = 1500  # time markers kept per transcript (5 s apart up to ~2 h, wider for longer meetings)


def _secs(ts: str) -> float:
    m = TIME_RX.fullmatch(ts.strip())
    if not m:
        return 0.0
    h, mi, s, frac = m.groups()
    return (int(h or 0) * 3600 + int(mi) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000 if frac else 0.0))


def hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# ----------------------------------------------------------------------------- parsers -> cues [(start, text)]
def parse_vtt(text: str) -> list[tuple[float, str]]:
    cues, cur_t, buf = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        m = VTT_CUE.search(line)
        if m:
            if cur_t is not None and buf:
                cues.append((cur_t, " ".join(buf)))
            cur_t, buf = _secs(m.group(1).replace(",", ".")), []
            continue
        if not line or line == "WEBVTT" or line.isdigit() or line.startswith(("NOTE", "STYLE", "REGION")):
            if not line and cur_t is not None and buf:
                cues.append((cur_t, " ".join(buf)))
                cur_t, buf = None, []
            continue
        if cur_t is not None:
            buf.append(TAG_RX.sub("", line).replace("&nbsp;", " ").strip())
    if cur_t is not None and buf:
        cues.append((cur_t, " ".join(buf)))
    return _dedupe(cues)


def parse_timestamped(text: str) -> list[tuple[float, str]]:
    """Text where lines start with (or contain) a time code: Granicus transcript viewer, pasted transcripts."""
    cues, cur_t, buf = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[?\(?((?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\)?\]?\s*[-–:]?\s*(.*)$", line)
        if m:
            if cur_t is not None and buf:
                cues.append((cur_t, " ".join(buf)))
            cur_t, buf = _secs(m.group(1)), ([m.group(2)] if m.group(2) else [])
        elif cur_t is not None:
            buf.append(line)
    if cur_t is not None and buf:
        cues.append((cur_t, " ".join(buf)))
    return _dedupe(cues)


def _dedupe(cues: list[tuple[float, str]]) -> list[tuple[float, str]]:
    out = []
    for t, txt in cues:
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt and (not out or out[-1][1] != txt):
            out.append((t, txt))
    return out


def cues_to_record(cues: list[tuple[float, str]], source_id: str, url: str, title: str, meta: dict) -> Record | None:
    """Join cues into text; keep one [seconds, char_offset] marker per ~30 s window so citations carry a time."""
    if not cues:
        return None
    parts, segments, pos, last_mark = [], [], 0, -1e9
    window = max(5.0, cues[-1][0] / MAX_MARKERS)
    for t, txt in cues:
        if t - last_mark >= window:
            segments.append([round(t, 1), pos])
            last_mark = t
        parts.append(txt)
        pos += len(txt) + 1
    text = "\n".join(parts)
    duration = cues[-1][0]
    return Record(source_id, "transcript", url, title=title, text=text, channel="html", evidence="primary",
                  meta={**meta, "segments": segments, "duration": hms(duration), "cues": len(cues), "opened": True})


def time_at(rec: dict, char_offset: int) -> str | None:
    """Time code for a character position in a transcript record's text."""
    segs = rec.get("meta", {}).get("segments") or []
    best = None
    for sec, off in segs:
        if off <= char_offset:
            best = sec
        else:
            break
    return hms(best) if best is not None else None


# ----------------------------------------------------------------------------- finding transcripts on a page
def transcript_links(html: str, base_url: str) -> list[str]:
    out = []
    soup = extract.soup_of(html)
    for tr in soup.find_all("track", src=True):
        out.append(urljoin(base_url, tr["src"]))
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if TRANSCRIPT_LINK.search(h) or re.search(r"transcript|closed caption", a.get_text(" ", strip=True), re.I):
            out.append(urljoin(base_url, h))
    for m in TRANSCRIPT_LINK.finditer(html):  # links inside scripts / player config
        out.append(urljoin(base_url, m.group(0)))
    return [u for u in dict.fromkeys(out) if re.search(r"TranscriptViewer|\.vtt|\.srt|transcript|caption", u, re.I)]


def extract_from_page(fetcher, sid: str, src: dict, r, html: str, meta: dict | None = None):
    """Channel hook: transcripts linked from a crawled HTML page (video pages, meeting pages)."""
    if not VIDEO_PAGE.search(r.url) and "<track" not in html and not re.search(r"\.(vtt|srt)\b", html, re.I) \
            and "TranscriptViewer" not in html:
        return
    meta = {**(meta or {}), "video_page": r.url}
    for link in transcript_links(html, r.url)[:4]:
        rec = fetch_transcript(fetcher, sid, link, title=meta.get("title") if meta else None, meta=meta)
        if rec:
            yield rec


def fetch_transcript(fetcher, sid: str, url: str, title: str | None = None, meta: dict | None = None) -> Record | None:
    try:
        r = fetcher.get(url)
    except (Disallowed, Blocked):
        return None
    if r.status != 200:
        return None
    body = r.text
    low_url = url.lower()
    if ".vtt" in low_url or body.lstrip().startswith("WEBVTT") or ".srt" in low_url or re.search(r"^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3} -->", body[:200], re.M):
        cues = parse_vtt(body)
        kind = "vtt"
    elif "html" in r.content_type:
        text = extract.soup_of(body).get_text("\n", strip=True)
        cues = parse_timestamped(text)
        kind = "granicus" if "TranscriptViewer" in url else "html"
        title = title or (extract.soup_of(body).title.get_text(strip=True) if extract.soup_of(body).title else "")
    else:
        cues = parse_timestamped(body)
        kind = "text"
    if len(cues) < 5:
        return None
    return cues_to_record(cues, sid, r.url, title or "Meeting transcript", {**(meta or {}), "transcript_source": kind,
                                                                           "video_page": (meta or {}).get("video_page")})


# ----------------------------------------------------------------------------- meetings with a video link
def from_meetings(fetcher, store, limit: int = 40, deadline: float | None = None, log=print) -> dict:
    """For meeting records that carry a video page (Legistar EventVideoPath, Granicus clips) and have no transcript
    yet: open the video page, take the transcript link it carries, read it."""
    out = {"checked": 0, "transcripts": 0}
    rows = store.query("kind='meeting' AND meta LIKE '%EventVideoPath%' AND meta NOT LIKE '%\"transcript_checked\"%'",
                       order="published DESC", limit=limit)
    for m in rows:
        if deadline and time.monotonic() > deadline:
            break
        video = (m["meta"].get("EventVideoPath") or "").strip()
        rec = Record(m["source_id"], m["kind"], m["url"], title=m["title"], text=m["text"], markdown=m["markdown"],
                     published=m["published"], channel=m["channel"], jurisdiction=m["jurisdiction"], evidence=m["evidence"],
                     meta={**m["meta"], "transcript_checked": dt.date.today().isoformat()}, key=m["key"])
        if video.startswith("http"):
            out["checked"] += 1
            try:
                r = fetcher.get(video)
                if r.status == 200 and "html" in r.content_type:
                    meta = {"video_page": r.url, "meeting_date": (m.get("published") or "")[:10],
                            "body": m["meta"].get("EventBodyName"), "EventId": m["meta"].get("EventId")}
                    for t in extract_from_page(fetcher, m["source_id"], {}, r, r.text, meta=meta):
                        t.jurisdiction = m["jurisdiction"]
                        t.title = f"{m['title']} — transcript"
                        if store.add(t):
                            out["transcripts"] += 1
                            rec.meta["transcript"] = t.url
            except (Disallowed, Blocked):
                rec.meta["transcript_checked"] = "disallowed"
            except Exception as exc:  # noqa: BLE001
                log(f"  {video}: {type(exc).__name__}: {exc}"[:200])
        store.add(rec)
    store.flush()
    return out


# ----------------------------------------------------------------------------- YouTube channel listing (Data API)
YT_API = "https://www.googleapis.com/youtube/v3"
# words that mark a channel as somebody else's, not the government's (title and handle only: a description can
# mention the council chambers or the local news)
NOT_GOVERNMENT = re.compile(r"chamber|news|church|ministr|foundation|visit|touris|cvb|convention|realt|democrat|"
                            r"republican|\bgop|party|unofficial|fan|alumni|booster|\bpta\b|radio|times|herald|patch|"
                            r"podcast|watchdog|citizens\s+for|friends\s+of|residents|neighbors|taxpayers", re.I)
CALL_SIGN = re.compile(r"\b[WK][A-Z]{2,3}(?:-(?:TV|FM|AM|DT))?\b")    # broadcast stations (WSB-TV, WXIA, ...)
GOVERNMENT = re.compile(r"\b(city|county|town|village|government|gov|govt|commission|council|board|schools?|"
                        r"district|consolidated|unified|municipal|clerk)\b|gov(?:t)?\b", re.I)


def youtube_channel(fetcher, handle_or_url: str) -> dict | None:
    """The channel behind a channel URL (@handle, /channel/ID, /c/name, /user/name or a bare youtube.com/name):
    {id, title, handle, description, uploads, url}. One 1-unit Data API call per form tried."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return None
    from .verified_posts import handle_of

    path = [x for x in urlsplit(handle_or_url).path.split("/") if x] if handle_or_url.startswith("http") else [handle_or_url]
    h = handle_of(handle_or_url) if handle_or_url.startswith("http") else handle_or_url
    tries = []
    if path and path[0] == "channel" and len(path) > 1:
        tries.append({"id": path[1]})
    elif h and h.startswith("@"):
        tries.append({"forHandle": h})
    else:
        name = (h or (path[-1] if path and path[0] not in ("c", "user") else (path[1] if len(path) > 1 else ""))).lstrip("@")
        if name:
            tries += [{"forHandle": "@" + name}, {"forUsername": name}]   # bare youtube.com/name: a handle or a legacy name
    for t in tries:
        data = fetcher.get(f"{YT_API}/channels", params={"part": "snippet,contentDetails", "key": key, **t}, use_cache=False).json()
        items = data.get("items") or []
        if items:
            it = items[0]
            sn = it.get("snippet") or {}
            return {"id": it["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl", ""),
                    "description": sn.get("description", ""), "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
                    "url": f"https://www.youtube.com/channel/{it['id']}"}
    return None


def _core(name: str) -> str:
    n = re.sub(r"\b(city|county|town|village|of|the|consolidated|unified|government|board|of|education|school|schools|"
               r"district|ga|georgia)\b", " ", (name or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", n))


def official_channel(ch: dict, jurisdiction_name: str, basis: str) -> tuple[bool, str]:
    """A channel is the government's official account when the government itself points to it (a link on its own
    website, found by the seed, or a hand check recorded in config/overrides.yaml) and the channel is plainly the
    government's: it carries the place's name or a government word, and nothing marking it as a chamber, a news
    outlet, a party or a group of residents."""
    if not basis:
        return False, "no official page links to this channel"
    title_handle = f"{ch.get('title', '')} {ch.get('handle', '')}"
    bad = NOT_GOVERNMENT.search(title_handle) or CALL_SIGN.search(ch.get("title") or "")
    if bad:
        return False, f"the channel's name ({ch.get('title') or ch.get('handle')}) marks it as not the government's ('{bad.group(0)}')"
    if basis.startswith("hand-checked"):
        return True, basis
    core = _core(jurisdiction_name)
    flat = re.sub(r"[^a-z0-9]", "", title_handle.lower())
    named = bool(core) and (core.replace(" ", "") in flat or core in (ch.get("description") or "").lower())
    gov = bool(GOVERNMENT.search(title_handle) or GOVERNMENT.search((ch.get("description") or "")[:300]))
    if named or gov:
        return True, f"{basis}; channel '{ch.get('title')}'" + (" names the place" if named else " is a government channel")
    return False, f"{basis}, but the channel '{ch.get('title')}' neither names the place nor reads as a government channel"


def youtube_pages(fetcher, ch: dict, since: str = "2016-01-01", start_token: str | None = None, limit: int | None = None):
    """A channel's uploads, newest first, one Data API page (50 videos, 1 quota unit) at a time: yields
    (videos, next_token); next_token is None once the listing reached `since` or the channel's end. `start_token`
    resumes an earlier listing (uploads made since only shift it: some videos are seen twice, none skipped); a
    token the API no longer accepts starts the listing over."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key or not ch or not ch.get("uploads"):
        return
    token, n, restarted = start_token or None, 0, False
    while True:
        p = {"part": "snippet", "playlistId": ch["uploads"], "maxResults": 50, "key": key}
        if token:
            p["pageToken"] = token
        r = fetcher.get(f"{YT_API}/playlistItems", params=p, use_cache=False)
        if r.status == 400 and token and not restarted:
            token, restarted = None, True          # a stale page token: list from the top again
            continue
        if r.status == 404 and not token:
            return                                 # the channel has no public uploads list: nothing to list
        if r.status != 200:
            raise RuntimeError(f"YouTube playlistItems: HTTP {r.status}")
        data = r.json()
        page, done = [], False
        for it in data.get("items", []):
            sn = it.get("snippet") or {}
            if (sn.get("publishedAt") or "")[:10] < since[:10]:
                done = True
                break
            vid = (sn.get("resourceId") or {}).get("videoId")
            if not vid:
                continue
            page.append({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": sn.get("title", ""),
                         "published": sn.get("publishedAt"), "description": sn.get("description", "")})
        n += len(page)
        nxt = None if done else data.get("nextPageToken")
        if limit is not None and n >= limit:
            nxt = None
        yield page, nxt
        if not nxt:
            return
        token = nxt


def youtube_videos(fetcher, handle_or_url: str, since: str = "2016-01-01", limit: int = 200, channel: dict | None = None):
    ch = channel or (youtube_channel(fetcher, handle_or_url) if os.environ.get("YOUTUBE_API_KEY") else None)
    out: list[dict] = []
    for page, _ in youtube_pages(fetcher, ch, since, limit=limit):
        out += page
    return out[:limit]


MEETING_TITLE = re.compile(r"council|commission|meeting|hearing|work session|board|retreat|budget", re.I)


FULL_LISTING = 5000     # videos listed on a channel's first pass (100 Data API units at most); later passes list new uploads
FULL_PASS = "2"         # the `full` cursor once a channel has been listed back to `since` by this reader
RUMOR_LISTING = 200     # a channel not confirmed official: only its newest videos go to the Rumor Mill


def youtube_records(fetcher, sid: str, jurisdiction: str, channel_url: str, since: str = "2016-01-01",
                    jurisdiction_name: str = "", basis: str = "", rumors=None, modified_since: str | None = None,
                    cursors: dict | None = None):
    """Meeting videos of an official channel as records; a channel that is not confirmed official goes to the
    Rumor Mill - Video bucket instead (nothing from it becomes a record).

    First pass: the channel listed back to `since` (up to FULL_LISTING videos; the old reader stopped at 200, which
    cut channels with years of meetings short), page by page: each page's meetings are records at once and its
    page token a checkpoint, so a run that ends mid-channel resumes there; a `full` checkpoint when it is done.
    Later passes list uploads since the last run. A channel the old reader marked done (no `full` checkpoint) gets
    the first pass too."""
    from .channels import Checkpoint

    ch = youtube_channel(fetcher, channel_url)
    if not ch:
        return
    ok, why = official_channel(ch, jurisdiction_name or jurisdiction, basis)
    if not ok:
        videos = [v for v in youtube_videos(fetcher, channel_url, since, limit=RUMOR_LISTING, channel=ch)
                  if MEETING_TITLE.search(v["title"])]
        if rumors is None:
            from .config import ROOT
            from .rumor_video import VideoRumorMill

            rumors = VideoRumorMill(ROOT)
        for v in videos:
            rumors.add(jurisdiction, ch, why, v)
        rumors.flush()
        return
    cursors = cursors or {}
    full = not (modified_since and cursors.get("full") == FULL_PASS)

    def records(page):
        for v in page:
            if MEETING_TITLE.search(v["title"]):
                yield Record(sid, "video", v["url"], title=v["title"], text=v["description"], published=v["published"],
                             channel="api", jurisdiction=jurisdiction, evidence="primary",
                             meta={"video_id": v["video_id"], "platform": "youtube", "opened": True,
                                   "official_channel": {"id": ch["id"], "title": ch["title"], "handle": ch["handle"], "basis": why},
                                   "transcript_status": "captions are not available through the Data API; read them on the video page"})

    if not full:
        for page, _ in youtube_pages(fetcher, ch, max(since, modified_since[:10]), limit=500):
            yield from records(page)
        return
    for page, nxt in youtube_pages(fetcher, ch, since, start_token=cursors.get("yt_page") or None, limit=FULL_LISTING):
        yield from records(page)
        yield Checkpoint("yt_page", nxt or "")
    yield Checkpoint("full", FULL_PASS)


# ----------------------------------------------------------------------------- optional local transcription
def transcribe_media(fetcher, url: str, max_mb: int = 300, model_size: str = "base", budget_seconds: float = 900.0):
    """Transcribe an audio/video file posted on an official site with faster-whisper (if installed)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    if not fetcher.allowed(url) or fetcher.blocklist.is_blocked(url):
        return None
    fetcher.limiter.wait(url, fetcher.robots.crawl_delay(url))
    with tempfile.NamedTemporaryFile(suffix=Path(urlsplit(url).path).suffix or ".mp4", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        with fetcher.session.get(url, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                return None
            size = 0
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_mb * 1e6:
                        return None
                    fh.write(chunk)
        model = WhisperModel(model_size, compute_type="int8")
        segments, _info = model.transcribe(str(path), vad_filter=True)
        t0, cues = time.monotonic(), []
        for seg in segments:
            cues.append((float(seg.start), seg.text.strip()))
            if time.monotonic() - t0 > budget_seconds:
                break
        return cues
    finally:
        path.unlink(missing_ok=True)


def transcribe_recordings(fetcher, store, cfg: dict, deadline: float | None = None, log=print) -> dict:
    """Official recordings (.mp3/.mp4 links on crawled pages) without a transcript yet, newest first."""
    opts = cfg.get("transcribe", {})
    out = {"checked": 0, "transcribed": 0}
    if not opts.get("enabled"):
        return out
    rows = store.query("kind IN ('page','meeting') AND (text LIKE '%.mp3%' OR text LIKE '%.mp4%' OR text LIKE '%.m4a%')",
                       order="published DESC", limit=int(opts.get("per_run", 3)))
    for pg in rows:
        if deadline and time.monotonic() > deadline:
            break
        for u in dict.fromkeys(re.findall(r"https?://[^\s)\]\"']+\.(?:mp3|m4a|wav|mp4|m4v)\b", pg["text"]))[:2]:
            if store.url_seen(u):
                continue
            out["checked"] += 1
            cues = transcribe_media(fetcher, u, int(opts.get("max_mb", 300)), opts.get("model", "base"),
                                    float(opts.get("budget_seconds", 900)))
            store.mark_url(u, 200 if cues else 0, pg["source_id"], "audio")
            if cues:
                rec = cues_to_record(cues, pg["source_id"], u, f"{pg['title']} — recording transcript",
                                     {"transcript_source": "whisper", "video_page": pg["url"]})
                if rec:
                    rec.jurisdiction = pg["jurisdiction"]
                    out["transcribed"] += store.add(rec)
    store.flush()
    return out


def _register():
    from . import channels

    if extract_from_page not in channels.PAGE_EXTRACTORS:
        channels.PAGE_EXTRACTORS.append(extract_from_page)


_register()
