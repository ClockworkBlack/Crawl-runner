"""Record store: one SQLite database (WAL) with full-text search.

Why SQLite instead of JSONL
- upserts by natural key: when a page changes, its row is updated (version+1)
  instead of a second copy piling up next to the first
- full-text search (FTS5, bm25 ranking, snippets) over every record, so
  "every official record that names this candidate" is a millisecond query
  rather than a scan of every file
- a URL frontier (urls table): documents already fetched are not fetched
  again, which is what makes re-runs and interrupted backfills cheap
- per-source cursors, so a long first-time download resumes where it stopped
- one file to persist between CI runs, and deltas that merge cleanly

Record ids
- key   natural key: source_id | kind | url | natural id (Legistar ids, guid...)
- id    sha256(key)[:24]: stable across content changes; use it to upsert downstream
- hash  sha256 of the content: changes when the content does
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .textfix import fix_text

SCHEMA_VERSION = 2
TOO_LARGE_RETRY_DAYS = 30      # a file over the size limit is not asked for again for this long (Store.fresh)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# meta fields that identify a record when several records share one URL
ID_FIELDS = ("EventItemId", "VoteId", "MatterId", "EventId", "OfficeRecordId", "event_item_id", "file_id", "event_id",
             "guid", "wp_id", "record_id", "tweet_id", "video_id", "uri", "account_id", "person_id")
# kinds where many distinct records legitimately share a URL and carry no id: keyed by content
CONTENT_KEYED = {"connection", "ocr"}


def norm_published(s):
    """A record's date as ISO 8601: "2026-10-27" or "2026-10-27T15:05:21-05:00". Feeds and some sites write the form
    of e-mail headers ("Tue, 27 Oct 2026 15:05:21 -0500"; 317 of 322,446 dated records in the Nov 14, 2026 bundle),
    which sorts before every ISO date in SQL and was cut to "Tue, 27 Oc" by readers that take a date's first ten
    characters. A form neither ISO nor that is returned as it is."""
    if not isinstance(s, str):
        return s or None
    s = s.strip()
    if not s:
        return None
    if s[:4].isdigit():
        return s
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError, OverflowError):
        return s
    return d.isoformat() if d else s


@dataclass
class Record:
    source_id: str
    kind: str            # ordinance | matter | meeting | agenda_item | vote | official | page | document | feed_item | ocr
                         # statement | article | mention | social_mention | post | connection | lead
    url: str
    title: str = ""
    text: str = ""
    markdown: str = ""
    published: str | None = None
    channel: str = ""    # api | wp_json | feed | sitemap | html | browser | pdf | ocr | import | store
    fetched_at: str = field(default_factory=utcnow)
    meta: dict = field(default_factory=dict)
    jurisdiction: str = ""   # registry id (co-fulton, canton, ...)
    evidence: str = ""       # primary | reported | unverified | lead
    key: str = ""            # natural key; computed when empty

    def __post_init__(self):
        # one date form and text as people read it, whichever source made the record (gacrawl/textfix.py)
        self.published = norm_published(self.published)
        self.title, self.text, self.markdown = fix_text(self.title), fix_text(self.text), fix_text(self.markdown)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for part in (self.source_id, self.kind, self.url, self.title, self.text or self.markdown,
                     json.dumps(self.meta, sort_keys=True, default=str)):
            h.update((part or "").encode())
            h.update(b"\0")
        return h.hexdigest()

    def natural_key(self) -> str:
        if self.key:
            return self.key
        nid = next((str(self.meta[f]) for f in ID_FIELDS if self.meta.get(f) not in (None, "")), "")
        if not nid and self.kind in CONTENT_KEYED:
            nid = "h:" + self.content_hash[:16]
        return f"{self.source_id}|{self.kind}|{self.url}|{nid}"

    @property
    def stable_id(self) -> str:
        return hashlib.sha256(self.natural_key().encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return {"id": self.stable_id, "hash": self.content_hash[:24], "source_id": self.source_id, "kind": self.kind,
                "url": self.url, "title": self.title, "text": self.text, "markdown": self.markdown,
                "published": self.published, "channel": self.channel, "fetched_at": self.fetched_at,
                "meta": self.meta, "jurisdiction": self.jurisdiction, "evidence": self.evidence}


DDL = """
CREATE TABLE IF NOT EXISTS records(
  rid INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  id TEXT NOT NULL,
  hash TEXT NOT NULL,
  source_id TEXT NOT NULL,
  jurisdiction TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  markdown TEXT NOT NULL DEFAULT '',
  published TEXT,
  channel TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  thash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id, kind);
CREATE INDEX IF NOT EXISTS records_jur ON records(jurisdiction, kind);
CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
CREATE INDEX IF NOT EXISTS records_changed ON records(changed_at);
CREATE INDEX IF NOT EXISTS records_url ON records(url);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  title, text, content='records', content_rowid='rid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, text) VALUES ('delete', old.rid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE OF title, text ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, text) VALUES ('delete', old.rid, old.title, old.text);
  INSERT INTO records_fts(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TABLE IF NOT EXISTS urls(
  url TEXT PRIMARY KEY,
  source_id TEXT NOT NULL DEFAULT '',
  status INTEGER,
  content_type TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS urls_fetched ON urls(fetched_at);
CREATE TABLE IF NOT EXISTS cursors(
  source_id TEXT NOT NULL,
  stream TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_id, stream)
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

COLS = ("key", "id", "hash", "source_id", "jurisdiction", "kind", "url", "title", "text", "markdown", "published",
        "channel", "evidence", "meta", "fetched_at", "first_seen", "changed_at", "version", "thash")


def text_hash(title: str | None, text: str | None) -> str:
    """Hash of what the full-text index holds: an update that leaves it alone needn't re-index the row."""
    return hashlib.blake2b(f"{title or ''}\0{text or ''}".encode(), digest_size=8).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("rid", None)
    d.pop("thash", None)
    d["meta"] = json.loads(d.get("meta") or "{}")
    # rows stored before Nov 14, 2026 are read the way new records are written (Record.__post_init__)
    if d.get("published"):
        d["published"] = norm_published(d["published"])
    for k in ("title", "text", "markdown"):
        if d.get(k):
            d[k] = fix_text(d[k])
    return d


class Store:
    """Thread-safe SQLite record store. `root` is a directory (db at root/crawl.db) or a .db path."""

    def __init__(self, root: Path, commit_every: int = 400, commit_seconds: float = 3.0):
        root = Path(root)
        self.path = root if root.suffix == ".db" else root / "crawl.db"
        self.root = self.path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-65536")  # 64 MB page cache
        # only write at startup when the schema is missing, so opening a database another process is
        # writing (e.g. `gacrawl search` during a run) never waits on its write lock
        have = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('records','urls','cursors','kv',"
                               "'records_fts')").fetchone()[0]
        if have < 5:
            self.db.executescript(DDL)
            self.db.execute("INSERT OR IGNORE INTO kv VALUES('schema', ?)", (str(SCHEMA_VERSION),))
        elif "thash" not in {r[1] for r in self.db.execute("PRAGMA table_info(records)")}:
            # one-time upgrade of a database made before the column existed (rows get it on their next update)
            self.db.execute("ALTER TABLE records ADD COLUMN thash TEXT NOT NULL DEFAULT ''")
        self._pending = 0
        self._last_commit = time.monotonic()
        self._commit_every, self._commit_seconds = commit_every, commit_seconds
        self._in_tx = False
        self._import_legacy_jsonl()

    # ---------------------------------------------------------------- transactions
    def _begin(self):
        if not self._in_tx:
            self.db.execute("BEGIN")
            self._in_tx = True

    def _maybe_commit(self, force: bool = False):
        self._pending += 1
        if force or self._pending >= self._commit_every or time.monotonic() - self._last_commit > self._commit_seconds:
            self.flush()

    def flush(self):
        with self._lock:
            if self._in_tx:
                self.db.execute("COMMIT")
                self._in_tx = False
            self._pending = 0
            self._last_commit = time.monotonic()

    def close(self):
        self.flush()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- records
    def add(self, rec: Record) -> bool:
        """Insert, or update when the content changed. True if new or changed."""
        key, h = rec.natural_key(), rec.content_hash[:24]
        th = text_hash(rec.title, rec.text)
        now = utcnow()
        with self._lock:
            self._begin()
            cur = self.db.execute("SELECT rid, hash, thash, published, jurisdiction, evidence FROM records WHERE key=?", (key,)).fetchone()
            if cur and cur["hash"] == h:
                # the hash covers the source, kind, address, title, text and meta, not these three columns: a record
                # sent again with a date it lacked, or a corrected one (the minutes deriver's "the heading's date
                # when the document has none"), or with its place or evidence filled in, was dropped as unchanged
                # and the stored row kept its empty or wrong value. A value that is given and differs is stored.
                fix = {c: v for c, v in (("published", rec.published), ("jurisdiction", rec.jurisdiction), ("evidence", rec.evidence))
                       if v and v != (cur[c] or "")}
                if fix:
                    self.db.execute(f"UPDATE records SET {', '.join(c + '=?' for c in fix)}, changed_at=?, version=version+1 WHERE rid=?",
                                    (*fix.values(), now, cur["rid"]))
                    self._maybe_commit()
                    return True
                self._maybe_commit()
                return False
            meta = json.dumps(rec.meta, ensure_ascii=False, default=str)
            if cur and cur["thash"] == th:
                # only meta/markdown/dates changed: leave title and text (and so the full-text index) untouched
                self.db.execute(
                    "UPDATE records SET id=?, hash=?, source_id=?, jurisdiction=?, kind=?, url=?, markdown=?, published=?, "
                    "channel=?, evidence=?, meta=?, fetched_at=?, changed_at=?, version=version+1 WHERE rid=?",
                    (rec.stable_id, h, rec.source_id, rec.jurisdiction or "", rec.kind, rec.url or "", rec.markdown or "",
                     rec.published, rec.channel or "", rec.evidence or "", meta, rec.fetched_at, now, cur["rid"]))
                self._maybe_commit()
                return True
            vals = (key, rec.stable_id, h, rec.source_id, rec.jurisdiction or "", rec.kind, rec.url or "",
                    rec.title or "", rec.text or "", rec.markdown or "", rec.published, rec.channel or "",
                    rec.evidence or "", meta, rec.fetched_at)
            if cur:
                self.db.execute(
                    "UPDATE records SET id=?, hash=?, source_id=?, jurisdiction=?, kind=?, url=?, title=?, text=?, "
                    "markdown=?, published=?, channel=?, evidence=?, meta=?, fetched_at=?, changed_at=?, "
                    "version=version+1, thash=? WHERE rid=?", vals[1:] + (now, th, cur["rid"]))
            else:
                self.db.execute(f"INSERT INTO records({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                                vals + (now, now, 1, th))
            self._maybe_commit()
            return True

    def add_many(self, recs: Iterable[Record]) -> int:
        return sum(self.add(r) for r in recs)

    def read(self, source_id: str, kind: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM records WHERE source_id=? AND kind=? ORDER BY rid",
                                   (source_id, kind)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def read_recent(self, source_id: str, kind: str, limit: int, where: str = "", params: tuple = ()) -> list[dict]:
        """The newest `limit` records of a source and kind (by published date), decoded; an index walk, not a full read."""
        sql = ("SELECT * FROM records WHERE source_id=? AND kind=?" + (f" AND ({where})" if where else "") +
               " ORDER BY published DESC, rid DESC LIMIT ?")
        with self._lock:
            rows = self.db.execute(sql, (source_id, kind) + tuple(params) + (int(limit),)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def raw(self, where: str, params: tuple = (), cols: str = "rid, url, title, text, published, meta") -> list[sqlite3.Row]:
        """Rows without decoding meta: for scans that only need a few columns (decode `meta` with json.loads if needed)."""
        with self._lock:
            return self.db.execute(f"SELECT {cols} FROM records WHERE {where} ORDER BY rid", params).fetchall()

    def distinct(self, cols: str, where: str = "1", params: tuple = ()) -> list[sqlite3.Row]:
        """The distinct combinations of a few columns or meta fields (json_extract(meta, '$.x')) over the matching rows:
        one grouped query where a pass over every row would decode each row's meta."""
        with self._lock:
            return self.db.execute(f"SELECT DISTINCT {cols} FROM records WHERE {where}", params).fetchall()

    def raw_iter(self, where: str, params: tuple = (), cols: str = "*", batch: int = 2000) -> Iterator[sqlite3.Row]:
        """Undecoded rows in rid order, streamed in batches (keyset pagination): exports that copy `meta` through."""
        last = 0
        while True:
            with self._lock:
                rows = self.db.execute(f"SELECT rid, {cols} FROM records WHERE rid>? AND ({where}) ORDER BY rid LIMIT {batch}",
                                       (last,) + tuple(params)).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1]["rid"]

    def query(self, where: str = "1", params: tuple = (), order: str = "rid", limit: int | None = None) -> list[dict]:
        sql = f"SELECT * FROM records WHERE {where} ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else "")
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def iter_query(self, where: str = "1", params: tuple = (), order: str = "rid", batch: int = 2000) -> Iterator[dict]:
        """Stream rows in batches (keyset pagination) so exports don't hold everything in memory."""
        last = 0
        while True:
            with self._lock:
                rows = self.db.execute(f"SELECT * FROM records WHERE rid>? AND ({where}) ORDER BY rid LIMIT {batch}",
                                       (last,) + tuple(params)).fetchall()
            if not rows:
                return
            for r in rows:
                yield _row_to_dict(r)
            last = rows[-1]["rid"]

    def kinds(self, source_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT kind FROM records WHERE source_id=? ORDER BY kind",
                                                  (source_id,))]

    def sources(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT source_id FROM records ORDER BY source_id")]

    def counts(self, by: str = "kind", where: str = "1", params: tuple = ()) -> dict:
        assert by in ("kind", "source_id", "jurisdiction", "evidence", "channel")
        with self._lock:
            return {r[0]: r[1] for r in self.db.execute(
                f"SELECT {by}, COUNT(*) FROM records WHERE {where} GROUP BY {by}", params)}

    def total(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # ---------------------------------------------------------------- full-text search
    @staticmethod
    def fts_phrase(*phrases: str) -> str:
        """An FTS5 query matching any of the phrases exactly (quotes escaped, punctuation tokenized away)."""
        out = []
        for p in phrases:
            toks = "".join(ch if ch.isalnum() else " " for ch in p).split()
            if toks:
                out.append('"' + " ".join(toks).replace('"', '""') + '"')
        return " OR ".join(dict.fromkeys(out))

    def search(self, match: str, where: str = "1", params: tuple = (), limit: int = 200,
               snippet_tokens: int = 40) -> list[dict]:
        """bm25-ranked full-text search; each hit carries a `snippet` of the text around the match."""
        if not match:
            return []
        sql = (f"SELECT r.*, snippet(records_fts, 1, '', '', ' … ', {int(snippet_tokens)}) AS snippet, "
               f"bm25(records_fts) AS score FROM records_fts JOIN records r ON r.rid = records_fts.rowid "
               f"WHERE records_fts MATCH ? AND ({where}) ORDER BY score LIMIT {int(limit)}")
        with self._lock:
            rows = self.db.execute(sql, (match,) + tuple(params)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def has(self, key: str) -> bool:
        """A record with this natural key exists (whatever its content)."""
        with self._lock:
            self._begin()
            return self.db.execute("SELECT 1 FROM records WHERE key=?", (key,)).fetchone() is not None

    def get_meta(self, key: str) -> dict | None:
        """The meta of the record with this natural key, or None."""
        with self._lock:
            self._begin()
            r = self.db.execute("SELECT meta FROM records WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
        except ValueError:
            return {}

    def add_new(self, rec: Record) -> bool:
        """Insert only when no record has this key; never overwrites (keeps meta added later, e.g. entity ids)."""
        if self.has(rec.natural_key()):
            return False
        return self.add(rec)

    # ---------------------------------------------------------------- frontier
    def url_seen(self, url: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()
        return dict(r) if r else None

    def fresh(self, url: str, max_age_days: float) -> bool:
        """Fetched successfully within max_age_days (so it can be skipped). A file found too large (413, noted by the
        readers when the download passed the size limit) counts as fresh for TOO_LARGE_RETRY_DAYS: it used to be
        downloaded up to the limit again on every run."""
        r = self.url_seen(url)
        if not r or r["status"] not in (200, 413):
            return False
        if r["status"] == 413:
            max_age_days = min(max_age_days, TOO_LARGE_RETRY_DAYS)
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(r["fetched_at"])
        return age.total_seconds() < max_age_days * 86400

    def mark_url(self, url: str, status: int, source_id: str = "", content_type: str = "", content: bytes | None = None):
        h = hashlib.sha256(content).hexdigest()[:24] if content is not None else ""
        now = utcnow()
        with self._lock:
            self._begin()
            prev = self.db.execute("SELECT hash, changed_at FROM urls WHERE url=?", (url,)).fetchone()
            changed = now if (not prev or (h and prev["hash"] != h)) else prev["changed_at"]
            self.db.execute("INSERT INTO urls(url, source_id, status, content_type, hash, fetched_at, changed_at) "
                            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET source_id=excluded.source_id, "
                            "status=excluded.status, content_type=excluded.content_type, "
                            "hash=CASE WHEN excluded.hash<>'' THEN excluded.hash ELSE urls.hash END, "
                            "fetched_at=excluded.fetched_at, changed_at=?",
                            (url, source_id, status, content_type, h, now, changed, changed))
            self._maybe_commit()

    # ---------------------------------------------------------------- cursors
    def cursors(self, source_id: str) -> dict[str, str]:
        with self._lock:
            return {r["stream"]: r["value"] for r in
                    self.db.execute("SELECT stream, value FROM cursors WHERE source_id=?", (source_id,))}

    def set_cursor(self, source_id: str, stream: str, value: str):
        with self._lock:
            self._begin()
            self.db.execute("INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(source_id, stream) DO UPDATE SET "
                            "value=excluded.value, updated_at=excluded.updated_at", (source_id, stream, value, utcnow()))
            self._maybe_commit(force=True)  # cursors are progress: make them durable at once

    def clear_cursors(self, source_id: str, keep: tuple[str, ...] = ()):
        """Forget where a source's backfill stood. `keep`: streams that mark a finished pass (a reader's `full`
        marker), which a completed backfill must not forget."""
        with self._lock:
            self._begin()
            if keep:
                self.db.execute(f"DELETE FROM cursors WHERE source_id=? AND stream NOT IN ({','.join('?' * len(keep))})",
                                (source_id, *keep))
            else:
                self.db.execute("DELETE FROM cursors WHERE source_id=?", (source_id,))
            self._maybe_commit(force=True)

    # ---------------------------------------------------------------- key/value (derivation cursors, settings)
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r[0] if r else default

    def kv_set(self, key: str, value: str):
        with self._lock:
            self._begin()
            self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self._maybe_commit(force=True)

    def normalize_published(self) -> int:
        """Once per database: dates stored in the e-mail-header form rewritten as ISO 8601 (norm_published), so SQL that
        sorts or compares by date (read_recent) sees one form. Later records are written that way. -> rows changed."""
        if self.kv_get("published_iso") == "1":
            return 0
        with self._lock:
            rows = self.db.execute("SELECT rid, published FROM records WHERE published IS NOT NULL AND published != '' "
                                   "AND substr(published, 1, 1) NOT BETWEEN '0' AND '9'").fetchall()
            n = 0
            self._begin()
            for r in rows:
                p = norm_published(r["published"])
                if p != r["published"]:
                    self.db.execute("UPDATE records SET published=? WHERE rid=?", (p, r["rid"]))
                    n += 1
            self.flush()
        self.kv_set("published_iso", "1")
        return n

    def changed_since(self, since: str | None, where: str = "1", params: tuple = (), batch: int = 1000) -> Iterator[dict]:
        """Records changed after `since` (ISO), oldest change first (changed_at, then rid), so a derivation that
        runs out of time can keep the last change it reached as its cursor and carry on from there next run."""
        # keyset pagination on (changed_at, rid); the first page is strictly after `since`, as a cursor means
        last_t, last_r = (since or ""), (2 ** 62 if since else 0)
        while True:
            with self._lock:
                rows = self.db.execute(
                    f"SELECT * FROM records WHERE (changed_at > ? OR (changed_at = ? AND rid > ?)) AND ({where}) "
                    f"ORDER BY changed_at, rid LIMIT {batch}", (last_t, last_t, last_r) + tuple(params)).fetchall()
            if not rows:
                return
            for r in rows:
                yield _row_to_dict(r)
            last_t, last_r = rows[-1]["changed_at"], rows[-1]["rid"]

    def forget_unrecorded_documents(self, since: str) -> int:
        """Frontier rows that say a PDF was read (status 200) since `since`, with no record of it: removed, so the
        file is read again. The frontier used to be marked before the file was parsed, and a run that died in the
        parse (the PDF reader's crashes, Nov. 7-18, 2026) left the file "read" for ten years with nothing stored."""
        with self._lock:
            self._begin()
            n = self.db.execute(
                "DELETE FROM urls WHERE status=200 AND fetched_at >= ? AND (content_type LIKE '%pdf%' OR lower(url) LIKE '%.pdf') "
                "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.url = urls.url)", (since,)).rowcount
            self._maybe_commit(force=True)
        return n

    def set_aside_text_dates(self, max_age_years: int = 15) -> dict:
        """Web pages dated by the first date of their own words when that date is more than max_age_years before the
        page was first read (profile items dated 1956-01-12 and 2001-09-11, Nov 2026): the date is the page's text,
        not its publication, so it is cleared and kept as meta.date_set_aside, and meta.date_from says "text". A page
        whose markup or feed gave the date (meta.date_from set) and documents (dated from their own fields) are left."""
        from .extract import text_date

        out = {"checked": 0, "set_aside": 0}
        now = utcnow()
        with self._lock:
            self._begin()
            rows = self.db.execute(
                "SELECT rid, published, first_seen, text, markdown, meta FROM records WHERE published IS NOT NULL AND published <> '' "
                "AND kind NOT IN ('document', 'meeting', 'vote', 'agenda_item', 'election_result', 'ballot_question', 'bill', 'filing', "
                "'contribution', 'expenditure', 'policy', 'ordinance', 'official') "
                "AND channel IN ('html', 'browser', 'feed', 'wp_json', 'archive', 'web', 'wayback') "
                "AND substr(published, 1, 10) < date(first_seen, ?)", (f"-{int(max_age_years)} years",)).fetchall()
            for r in rows:
                out["checked"] += 1
                try:
                    meta = json.loads(r["meta"] or "{}")
                except ValueError:
                    continue
                if meta.get("date_from") and meta.get("date_from") != "text":
                    continue
                d = (r["published"] or "")[:10]
                if text_date(r["text"] or r["markdown"] or "") != d:
                    continue                                      # not the first date of its words: from elsewhere
                meta = {**meta, "date_from": "text", "date_set_aside": d}
                self.db.execute("UPDATE records SET published=NULL, meta=?, changed_at=?, version=version+1 WHERE rid=?",
                                (json.dumps(meta, ensure_ascii=False, sort_keys=True), now, r["rid"]))
                out["set_aside"] += 1
            self._maybe_commit(force=True)
        return out

    def rekey_feed_pages(self) -> dict:
        """Pages read through a feed or posts API used to carry the item's guid or post id in their key, and the same
        address read by the coverage crawl had none: two records of one page. Each such record is filed under the
        address-only key; where that key is already taken, the one changed last is kept."""
        out = {"rekeyed": 0, "dropped": 0}
        with self._lock:
            self._begin()
            rows = self.db.execute(
                "SELECT rid, key, source_id, kind, url, meta, changed_at FROM records WHERE channel IN ('feed', 'wp_json') "
                "AND kind <> 'feed_item' AND url <> '' AND (meta LIKE '%\"guid\"%' OR meta LIKE '%\"wp_id\"%')").fetchall()
            for r in rows:
                try:
                    meta = json.loads(r["meta"] or "{}")
                except ValueError:
                    continue
                nid = next((str(meta[f]) for f in ID_FIELDS if meta.get(f) not in (None, "")), "")
                field_ = next((f for f in ID_FIELDS if meta.get(f) not in (None, "")), "")
                if field_ not in ("guid", "wp_id") or r["key"] != f"{r['source_id']}|{r['kind']}|{r['url']}|{nid}":
                    continue
                new_key = f"{r['source_id']}|{r['kind']}|{r['url']}|"
                meta["feed_guid" if field_ == "guid" else "wp_post_id"] = meta.pop(field_)
                other = self.db.execute("SELECT rid, changed_at FROM records WHERE key=?", (new_key,)).fetchone()
                if other and (other["changed_at"] or "") >= (r["changed_at"] or ""):
                    self.db.execute("DELETE FROM records WHERE rid=?", (r["rid"],))
                    out["dropped"] += 1
                    continue
                if other:
                    self.db.execute("DELETE FROM records WHERE rid=?", (other["rid"],))
                    out["dropped"] += 1
                self.db.execute("UPDATE records SET key=?, id=?, meta=?, changed_at=? WHERE rid=?",
                                (new_key, hashlib.sha256(new_key.encode()).hexdigest()[:24],
                                 json.dumps(meta, ensure_ascii=False, default=str), utcnow(), r["rid"]))
                out["rekeyed"] += 1
            self._maybe_commit(force=True)
        return out

    def prune_prefix(self, prefix: str, keep: set[str]) -> int:
        """Remove records whose key starts with `prefix`, except the keys in `keep` (a deriver's records of one source
        record that a re-parse did not make again). A range on the key index, not a scan."""
        with self._lock:
            self._begin()
            rows = self.db.execute("SELECT rid, key FROM records WHERE key >= ? AND key < ?",
                                   (prefix, prefix + "\U0010ffff")).fetchall()
            gone = [r["rid"] for r in rows if r["key"].startswith(prefix) and r["key"] not in keep]
            for i in range(0, len(gone), 500):
                part = gone[i:i + 500]
                self.db.execute(f"DELETE FROM records WHERE rid IN ({','.join('?' * len(part))})", part)
            self._maybe_commit()
        return len(gone)

    def delete_where(self, where: str, params: tuple = ()) -> int:
        with self._lock:
            self._begin()
            n = self.db.execute(f"DELETE FROM records WHERE {where}", params).rowcount
            self._maybe_commit(force=True)
        return n

    def purge_source(self, source_id: str, channel: str | None = None) -> dict:
        """Remove a source entirely: its records (the search index follows by trigger), research mentions that cited
        them, its cursors and its frontier rows. For a portal that turned out to belong to another place.
        With `channel`, only the records that came through that channel go (with the mentions that cite their URLs
        and the source's cursors): a county site that once read the county's Legistar portal a second time keeps
        its own pages."""
        if channel:
            with self._lock:
                self._begin()
                out = {"citing_mentions": self.db.execute(
                    "DELETE FROM records WHERE source_id LIKE 'cand:%' AND json_extract(meta, '$.source')=? AND url IN "
                    "(SELECT url FROM records WHERE source_id=? AND channel=?)", (source_id, source_id, channel)).rowcount}
                out["records"] = self.db.execute("DELETE FROM records WHERE source_id=? AND channel=?",
                                                 (source_id, channel)).rowcount
                if out["records"]:
                    self.db.execute("DELETE FROM cursors WHERE source_id=?", (source_id,))
                self._maybe_commit(force=True)
            return out
        with self._lock:
            self._begin()
            out = {"records": self.db.execute("DELETE FROM records WHERE source_id=?", (source_id,)).rowcount,
                   "citing_mentions": self.db.execute("DELETE FROM records WHERE source_id LIKE 'cand:%' AND "
                                                      "json_extract(meta, '$.source')=?", (source_id,)).rowcount}
            self.db.execute("DELETE FROM cursors WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM urls WHERE source_id=?", (source_id,))
            self._maybe_commit(force=True)
        return out

    # ---------------------------------------------------------------- legacy JSONL (v0.1/v0.2 data/<source>/<kind>.jsonl)
    def _import_legacy_jsonl(self):
        # only the old record files: not the model's training logs (data/train), probes or other notes kept as JSONL
        files = [p for p in self.root.glob("*/*.jsonl")
                 if p.parent.name not in ("verified", "rumor_mill", "train", "openrecords") and not p.parent.name.startswith("ethics")]
        if not files or self.db.execute("SELECT v FROM kv WHERE k='legacy_imported'").fetchone():
            return
        self.db.execute("INSERT OR REPLACE INTO kv VALUES('legacy_imported', 'started')")
        for p in files:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or not d.get("source_id") or not d.get("kind"):
                    continue            # not a record line
                self.add(Record(d["source_id"], d["kind"], d.get("url", ""), title=d.get("title", ""),
                                text=d.get("text", ""), markdown=d.get("markdown", ""), published=d.get("published"),
                                channel=d.get("channel", ""), fetched_at=d.get("fetched_at") or utcnow(),
                                meta=d.get("meta") or {}, jurisdiction=d.get("jurisdiction", ""),
                                evidence=d.get("evidence", "")))
        self.db.execute("INSERT OR REPLACE INTO kv VALUES('legacy_imported', ?)", (utcnow(),))
        self.flush()
