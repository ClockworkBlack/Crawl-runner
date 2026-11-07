"""YouTube: only official government channels become records; anything else goes to the Rumor Mill - Video bucket.
Bare youtube.com/name links in a site's social icons are found; single videos and embeds are not channels."""
import json

from gacrawl import transcripts
from gacrawl.registry import find_portals, sources_for
from gacrawl.rumor_video import VideoRumorMill
from gacrawl.transcripts import official_channel

SITE = "linked from the official website (https://www.cityofsouthfultonga.gov/)"


def test_official_rules():
    ok, why = official_channel({"title": "City of South Fulton", "handle": "@cityofsouthfulton"}, "South Fulton", SITE)
    assert ok and "names the place" in why
    assert official_channel({"title": "Cobb County Government", "handle": "@cobbcountygovt"}, "Cobb County", SITE)[0]
    # the county site links the Chamber of Commerce's channel: not the government's
    ok, why = official_channel({"title": "Douglas County Chamber", "handle": "@dcchamber"}, "Douglas County",
                               "linked from the official website (http://www.douglascountygeorgia.com/)")
    assert not ok and "chamber" in why.lower()
    assert not official_channel({"title": "WSB-TV", "handle": "@wsbtv"}, "Atlanta", SITE)[0]
    assert not official_channel({"title": "South Fulton Residents United", "handle": "@sfru"}, "South Fulton", SITE)[0]
    # no official page points to it: never official, whatever it is called
    assert not official_channel({"title": "City of South Fulton", "handle": "@x"}, "South Fulton", "")[0]
    assert official_channel({"title": "CSF TV", "handle": "@csf"}, "South Fulton", "hand-checked: 2026-11-06 ...")[0]


def test_channel_links_found():
    assert find_portals('<a href="https://www.youtube.com/cityofsouthfulton">', "https://x.gov/") == \
        {"youtube": "https://www.youtube.com/cityofsouthfulton"}
    assert find_portals('<a href="https://www.youtube.com/watch?v=Z1F9POTRCTE">', "https://x.gov/") == {}
    assert find_portals('<iframe src="https://www.youtube.com/embed/xyz">', "https://x.gov/") == {}
    src = [s for s in sources_for({"id": "south-fulton", "name": "South Fulton", "tier": "a", "kind": "municipality", "website": "https://www.cityofsouthfultonga.gov/",
                                   "portals": {"youtube": "https://www.youtube.com/@cityofsouthfulton"}}) if s["kind"] == "video"][0]
    assert src["youtube_basis"] == "linked from the official website (https://www.cityofsouthfultonga.gov/)"


def test_unofficial_channel_goes_to_rumor_mill(tmp_path, monkeypatch):
    chans = {"official": {"id": "UC1", "title": "City of South Fulton", "handle": "@cityofsouthfulton", "description": "",
                          "uploads": "UU1", "url": "https://www.youtube.com/channel/UC1"},
             "other": {"id": "UC2", "title": "South Fulton News Now", "handle": "@sfnews", "description": "",
                       "uploads": "UU2", "url": "https://www.youtube.com/channel/UC2"}}
    vids = [{"video_id": "Z1F9POTRCTE", "url": "https://www.youtube.com/watch?v=Z1F9POTRCTE", "title": "City Council Meeting 10/27/2026",
             "published": "2026-10-27T22:00:00Z", "description": ""},
            {"video_id": "q", "url": "https://www.youtube.com/watch?v=q", "title": "Holiday parade", "published": "2026-10-01T00:00:00Z",
             "description": ""}]
    monkeypatch.setattr(transcripts, "youtube_channel", lambda f, u: chans[u])
    monkeypatch.setattr(transcripts, "youtube_videos", lambda f, u, since, limit=200, channel=None: [dict(v, url=v["url"] + u) for v in vids])
    monkeypatch.setattr(transcripts, "youtube_pages", lambda f, ch, since, start_token=None, limit=None: iter([(vids, None)]))
    rm = VideoRumorMill(tmp_path)
    from gacrawl.channels import Checkpoint

    recs = [r for r in transcripts.youtube_records(None, "south-fulton:youtube", "south-fulton", "official", jurisdiction_name="South Fulton",
                                                   basis=SITE, rumors=rm) if not isinstance(r, Checkpoint)]
    assert [r.title for r in recs] == ["City Council Meeting 10/27/2026"] and recs[0].meta["official_channel"]["id"] == "UC1"
    assert not list(transcripts.youtube_records(None, "south-fulton:youtube", "south-fulton", "other", jurisdiction_name="South Fulton",
                                                basis=SITE, rumors=rm))
    rows = [json.loads(l) for l in (tmp_path / "data" / "rumor_mill" / "videos.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["status"] == "unverified" and "news" in rows[0]["reason"].lower()
    assert rows[0]["channel"]["title"] == "South Fulton News Now"


def test_first_pass_lists_the_whole_channel_page_by_page_and_resumes(monkeypatch):
    """First pass: back to 2016, each page's meetings at once with its page token as a checkpoint, a `full` marker at
    the end; a run cut off mid-channel resumes at its token; once the marker is there, later passes list new uploads
    only. A channel the old reader marked done (last_ok, but no marker) still gets the first pass."""
    from gacrawl.channels import Checkpoint

    calls = []
    ch = {"id": "UC1", "title": "City of South Fulton", "handle": "@cityofsouthfulton", "description": "", "uploads": "UU1",
          "url": "https://www.youtube.com/channel/UC1"}
    monkeypatch.setattr(transcripts, "youtube_channel", lambda f, u: ch)
    pages = {None: ([{"video_id": "a", "url": "u/a", "title": "City Council Meeting", "published": "2026-10-01", "description": ""},
                     {"video_id": "b", "url": "u/b", "title": "Parade", "published": "2026-09-01", "description": ""}], "T2"),
             "T2": ([{"video_id": "c", "url": "u/c", "title": "Work Session", "published": "2019-01-01", "description": ""}], None)}

    def listing(f, chan, since, start_token=None, limit=None):
        calls.append((since, start_token, limit))
        tok = start_token
        while True:
            page, nxt = pages[tok]
            yield page, nxt
            if not nxt:
                return
            tok = nxt
    monkeypatch.setattr(transcripts, "youtube_pages", listing)
    args = ("s:youtube", "south-fulton", "x")
    kw = {"jurisdiction_name": "South Fulton", "basis": SITE}
    out = list(transcripts.youtube_records(None, *args, **kw))
    assert [r.url for r in out if not isinstance(r, Checkpoint)] == ["u/a", "u/c"]           # meetings only
    assert [(c.stream, c.value) for c in out if isinstance(c, Checkpoint)] == [("yt_page", "T2"), ("yt_page", ""), ("full", transcripts.FULL_PASS)]
    assert calls[-1] == ("2016-01-01", None, transcripts.FULL_LISTING) and transcripts.FULL_LISTING >= 2000
    # cut off after the first page last time: resumes at its token
    list(transcripts.youtube_records(None, *args, **kw, cursors={"yt_page": "T2"}))
    assert calls[-1][1] == "T2"
    # the old reader's last_ok, no marker: still the first pass
    list(transcripts.youtube_records(None, *args, **kw, modified_since="2026-11-05T04:07:00+00:00"))
    assert calls[-1] == ("2016-01-01", None, transcripts.FULL_LISTING)
    # marker present: new uploads only
    out = list(transcripts.youtube_records(None, *args, **kw, modified_since="2026-11-05T04:07:00+00:00",
                                           cursors={"full": transcripts.FULL_PASS}))
    assert calls[-1] == ("2026-11-05", None, 500) and not any(isinstance(c, Checkpoint) for c in out)


def test_page_listing_stops_at_since_and_restarts_a_stale_token(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    seen = []

    class R:
        def __init__(self, status, data):
            self.status, self._d = status, data

        def json(self):
            return self._d

    class F:
        def get(self, url, params=None, **kw):
            seen.append(params.get("pageToken"))
            if params.get("pageToken") == "STALE":
                return R(400, {})
            if params.get("pageToken") is None:
                return R(200, {"items": [{"snippet": {"publishedAt": "2026-10-01T00:00:00Z", "title": "Council", "resourceId": {"videoId": "a"}}}],
                               "nextPageToken": "P2"})
            return R(200, {"items": [{"snippet": {"publishedAt": "2015-06-01T00:00:00Z", "title": "Old", "resourceId": {"videoId": "z"}}}],
                           "nextPageToken": "P3"})
    out = list(transcripts.youtube_pages(F(), {"uploads": "UU1"}, "2016-01-01", start_token="STALE"))
    assert seen == ["STALE", None, "P2"]                       # stale token -> from the top; stops at 2016
    assert [([v["video_id"] for v in p], t) for p, t in out] == [(["a"], "P2"), ([], None)]
