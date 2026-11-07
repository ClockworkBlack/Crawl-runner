"""CivicClerk adapter (portal.civicclerk.com).

CivicClerk exposes a public OData feed at https://<client>.api.civicclerk.com/v1/
behind the portal UI. This reads Events (meetings) and the files published with
each one (agenda, packet, minutes), which come embedded in the event itself
(`publishedFiles`; the per-event sub-collections answer 404).

What a probe of a real deployment showed (kingslandga, November 2026): the server
pages 15 events at a time whatever `$top` asks for, `$skip` works, `$count=true`
gives the total (577 there), `$filter` takes startDateTime and createdOn, and
`$expand` is refused. So this reader pages until the total is reached, walks a
first full pass in year windows with a Checkpoint after each (an interrupted
backfill resumes at the next window, as the Legistar reader does), and on later
runs re-reads recent meetings (minutes arrive weeks later) plus any event created
since the last run. A file already opened is not fetched again (the frontier
remembers it; documents do not change once posted).

Field names vary a little between CivicClerk versions, so parsing is tolerant:
each value is looked up under several candidate keys and missing ones are left
blank. The adapter is purely additive — every jurisdiction also has its website
(`:site`) source — so if a deployment's API shape differs, nothing is lost.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterator

from ..fetch import Fetcher
from ..store import Record

API = "https://{client}.api.civicclerk.com/v1"
PAGE = 200            # asked for; the server may hand out fewer per page (15 seen) and is paged until the total
FULL_PASS = "2"       # the `full` cursor once this reader has walked a portal from `since`; a new value re-walks all
DOC_TTL_DAYS = 3650   # a published file is opened once


def _first(d: dict, *keys, default=""):
    for k in keys:
        for cand in (k, k[0].lower() + k[1:], k[0].upper() + k[1:]):
            if cand in d and d[cand] not in (None, ""):
                return d[cand]
    return default


def _year_windows(start: str, end: str) -> list[tuple[str, str]]:
    """[start, Jan 1 next year), [Jan 1, Jan 1 next) ... up to end (exclusive). ISO dates."""
    out, a = [], start[:10]
    while a < end:
        b = min(f"{int(a[:4]) + 1}-01-01", end)
        out.append((a, b))
        a = b
    return out


def _doc_type(kind: str) -> str:
    k = (kind or "").lower()
    if "minute" in k:
        return "minutes"
    if "packet" in k:
        return "packet"
    if "agenda" in k:
        return "agenda"
    return k[:40]


class CivicClerk:
    def __init__(self, fetcher: Fetcher, client: str):
        self.f = fetcher
        self.client = client
        self.base = API.format(client=client)

    def _get(self, path: str, params: dict | None = None):
        r = self.f.get(self.base + path, params=params, headers={"Accept": "application/json"})
        if r.status != 200:
            raise RuntimeError(f"CivicClerk {self.client}{path}: HTTP {r.status}")
        return r.json()

    def probe(self) -> int:
        data = self._get("/Events", {"$top": 5})
        return len(data.get("value", data if isinstance(data, list) else []))

    def count(self, flt: str | None = None) -> int | None:
        """How many events match (the server's own count), or None when it does not say."""
        params = {"$top": 1, "$count": "true"}
        if flt:
            params["$filter"] = flt
        data = self._get("/Events", params)
        n = data.get("@odata.count") if isinstance(data, dict) else None
        return int(n) if isinstance(n, (int, str)) and str(n).isdigit() else None

    def _rows(self, path: str, flt: str | None, order: str, limit: int | None) -> Iterator[dict]:
        """Every row, however the server pages: it may cap the page size below $top, so paging goes by what
        came back, until the server's total is reached (or a page comes back empty)."""
        skip, n, total = 0, 0, None
        while True:
            params = {"$top": PAGE, "$skip": skip, "$orderby": order}
            if total is None:
                params["$count"] = "true"
            if flt:
                params["$filter"] = flt
            data = self._get(path, params)
            rows = data.get("value", data) if isinstance(data, dict) else data
            if total is None and isinstance(data, dict):
                c = data.get("@odata.count")
                total = int(c) if isinstance(c, (int, str)) and str(c).isdigit() else -1   # -1: no total given
            if not rows:
                return
            for row in rows:
                yield row
                n += 1
                if limit and n >= limit:
                    return
            skip += len(rows)
            if total is not None and total >= 0 and skip >= total:
                return

    def meetings(self, since: str, limit: int | None = None, modified_since: str | None = None,
                 cursors: dict | None = None, lookback_days: int = 45, store=None) -> Iterator[Record]:
        """Meetings and their published files.

        First pass: every event from `since` in year windows, oldest first, with a Checkpoint after each window (an
        interrupted pass resumes there) and a `full` checkpoint once it is done. Later passes: events of the last
        `lookback_days` (minutes are posted weeks after a meeting) and anything scheduled or created since the last
        run. A source whose last run was the old reader's (15 meetings deep, no `full` checkpoint) gets the first
        pass too, though the scheduler sees it as done."""
        from ..channels import Checkpoint

        today = dt.date.today()
        if modified_since and (cursors or {}).get("full") == FULL_PASS:
            recent = (today - dt.timedelta(days=lookback_days)).isoformat()
            floor = min(modified_since[:10], recent)
            created = modified_since[:19] if "T" in modified_since else modified_since[:10] + "T00:00:00"
            filters = [f"startDateTime ge {floor}T00:00:00Z or createdOn ge {created}Z",   # a meeting entered late
                       f"startDateTime ge {floor}T00:00:00Z"]                               # if createdOn is not filterable
            for i, flt in enumerate(filters):
                try:
                    for e in self._rows("/Events", flt, "startDateTime desc", limit):
                        yield from self._one(e, store)
                    return
                except RuntimeError as exc:
                    if i == 0 and "HTTP 400" in str(exc):
                        continue
                    raise
            return
        start = (cursors or {}).get("events") or since
        end = (today + dt.timedelta(days=400)).isoformat()      # scheduled meetings too
        for a, b in _year_windows(start, end):
            flt = f"startDateTime ge {a}T00:00:00Z and startDateTime lt {b}T00:00:00Z"
            for e in self._rows("/Events", flt, "startDateTime", limit):
                yield from self._one(e, store)
            yield Checkpoint("events", b)
        yield Checkpoint("full", FULL_PASS)     # later runs are deltas (kept when the scheduler keeps cursors)

    def _one(self, e: dict, store=None) -> Iterator[Record]:
        if e.get("isDeleted"):
            return
        eid = _first(e, "id", "eventId")
        start = _first(e, "startDateTime", "eventDate", "date")
        name = _first(e, "eventName", "name", "categoryName", "title")
        loc = e.get("eventLocation")
        if isinstance(loc, dict):
            # as the API gives it: "101 Main Street, Villa Rica, GA 30180" (the state is part of the place: Electo's
            # check that a portal is about this Georgia place reads it)
            street = ", ".join(str(loc[k]).strip() for k in ("address1", "address2", "city") if str(loc.get(k) or "").strip())
            tail = " ".join(str(loc[k]).strip() for k in ("state", "zipCode") if str(loc.get(k) or "").strip())
            where = ", ".join(x for x in (street, tail) if x)
        else:
            where = str(_first(e, "location", "eventLocation"))
        # cite the API resource that was read (or a link the API itself gives), never a built page URL
        page = _first(e, "eventUrl", "url", "portalUrl") or (f"{self.base}/Events({eid})" if eid else f"{self.base}/Events")
        yield Record(source_id=self.client, kind="meeting", channel="api", url=page,
                     title=f"{name} {str(start)[:10]}".strip(), published=start or None,
                     text=" · ".join(str(x) for x in (name, str(start)[:10], where) if x),
                     meta={"event_id": eid, "category": _first(e, "categoryName", "eventCategoryName", "category"),
                           "location": where, "opened": True, "provider": "civicclerk",
                           "has_agenda": bool(e.get("hasAgenda")), "has_media": bool(e.get("hasMedia")),
                           "created_on": str(_first(e, "createdOn"))[:19] or None})
        files = e.get("publishedFiles")
        if files is None:
            files = e.get("eventFiles") or e.get("files") or []
        for fdoc in files or []:
            rec = self._open_file(fdoc, name, start, eid, store)
            if rec:
                yield rec

    def _open_file(self, fdoc: dict, name: str, start, eid, store=None):
        """Fetch a published file and record what was actually opened.

        Only URLs the API itself gives are used (no pattern-built links), and a
        file becomes a record only after it was opened (HTTP 200); its text is
        extracted so the record is citeable and searchable. A file opened on an
        earlier run (the frontier remembers it) is not fetched again."""
        from urllib.parse import urljoin

        from ..blocked import Blocked
        from ..fetch import Disallowed, OutOfTime, TooLarge

        url = _first(fdoc, "url", "downloadUrl", "fileUrl")
        ftype = str(_first(fdoc, "type", "fileType", default="") or "")
        fname = _first(fdoc, "name", "fileName", default="") or ftype or "document"
        if not url:
            return None
        url = urljoin(self.base + "/", str(url))   # the API gives some links relative ("stream/<CLIENT>/<id>.pdf")
        if store is not None and store.fresh(url, DOC_TTL_DAYS):
            return None                            # opened before: its record is in the store
        try:
            r = self.f.get(url)
        except OutOfTime:
            raise
        except (Disallowed, Blocked, TooLarge):
            return None
        except Exception:  # noqa: BLE001 — one file the server can't hand over doesn't end the meeting list
            return None
        if store is not None:
            store.mark_url(url, r.status, self.client, r.content_type)
        if r.status != 200:
            return None
        meta = {"event_id": eid, "file_id": _first(fdoc, "fileId", "id"), "file_name": fname, "opened": True,
                "doc_type": _doc_type(ftype if not ftype.isdigit() else fname),
                "published_on": str(_first(fdoc, "publishOn"))[:19] or None}
        title = f"{name}: {fname}".strip()
        if r.content_type == "application/pdf" or r.content[:5] == b"%PDF-":
            from ..ingest import pdf_to_record

            try:
                rec = pdf_to_record(r.content, source_id=self.client, kind="document", url=r.url, title=title, meta=meta,
                                    stop_at=self.f.deadline())
            except OutOfTime:
                raise
            except Exception as exc:  # noqa: BLE001 — one unreadable file must not end the meeting list
                rec = Record(source_id=self.client, kind="document", channel="api", url=r.url, title=title, text="",
                             meta={**meta, "unreadable": f"{type(exc).__name__}: {exc}"[:200]})
            rec.published = start or None
            return rec
        from ..extract import html_to_markdown

        _, md = html_to_markdown(r.text, r.url) if "html" in r.content_type else ("", r.text[:200000])
        return Record(source_id=self.client, kind="document", channel="api", url=r.url, title=title,
                      text=md, markdown=md, published=start or None, meta=meta)
