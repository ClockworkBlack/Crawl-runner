import json
from pathlib import Path

from gacrawl import newsdigest as nd
from gacrawl.store import Store


def test_keep_and_tags():
    assert nd.keep("Gwinnett commission approves budget", "The county commission voted 4-1.")
    assert nd.keep("Kemp signs order", "")
    assert not nd.keep("Braves win in extra innings", "A walk-off homer in Atlanta.")
    assert "budget" in nd.tags_of("Gwinnett commission approves budget", "")


def test_excerpt_trims():
    assert nd._excerpt("One. Two. Three.") == "One. Two."
    assert nd._excerpt("A story. The post A story appeared first on X.") == "A story."


def test_export(tmp_path):
    import datetime as dt
    from gacrawl.store import Record

    s = Store(tmp_path / "t.db")
    d = dt.date.today().isoformat()
    s.add(Record(nd.SOURCE, "news_item", "https://ex.com/a", title="Council passes ordinance", text="x.", published=d,
                 meta={"outlet_name": "Ex News", "tags": ["local"]}))
    s.add(Record(nd.SOURCE, "news_item", "https://ex.com/old", title="Old council story", published="2020-01-01"))
    s.flush()
    out = tmp_path / "news.json"
    c = nd.export_news(s, out)
    j = json.loads(out.read_text())
    assert c["items"] == 1 and j["items"][0]["s"] == "Ex News" and j["items"][0]["d"] == d and j["updated"]
