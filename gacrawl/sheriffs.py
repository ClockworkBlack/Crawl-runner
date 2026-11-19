"""Every county's sitting sheriff, from the Georgia Sheriffs' Association's "Sheriffs by County" list (`gacrawl police
sheriffs`, also in `police all`) -> state/sheriffs.json, once a week.

The association's members are Georgia's 159 elected sheriffs, and its list gives each county's sheriff, the office's
address and its phone: public facts, printed for anyone. The Secretary of State's results say who was elected; this
says who holds the office now (a sheriff appointed after a death or a resignation, a special election's winner not yet
in the results), and it is what a sheriff's office page read with someone else's name is checked against.

The page is read from the association's site; where the site refuses the crawler (a 403, a bot check, robots.txt) or
answers without the list, from the Internet Archive's newest copy of it within ARCHIVE_DAYS (the owner's rule since
Nov 19, 2026: a public page that refuses the crawler may be read from the Wayback Machine's existing copies; never
asking the Archive to capture it, never solving a challenge). Each county's row keeps where it was read (`src`) and,
for an archive copy, the capture day (`captured`). A list with fewer than MIN_COUNTIES counties is not believed, and
the last one stands.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from pathlib import Path

from .registry import write_json_atomic

URL = "https://georgiasheriffs.org/resources/sheriffs-by-county/"
SITE = "Georgia Sheriffs' Association"
REFRESH_DAYS = 7
ARCHIVE_DAYS = 400
MIN_COUNTIES = 120
PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b")
TITLE = re.compile(r"^\s*(?:the\s+)?(?:interim\s+|acting\s+)?sheriff\s+", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower().replace("county", ""))


def county_key(name: str, counties: list[str]) -> str | None:
    """The registry's county a printed county name is ("McDUFFlE", with a lower-case L for the i, is McDuffie)."""
    want = {_norm(c): c for c in counties}
    n = _norm(name)
    if n in want:
        return want[n]
    n2 = n.replace("l", "i")
    for k, c in want.items():
        if k.replace("l", "i") == n2:
            return c
    close = difflib.get_close_matches(n, list(want), n=1, cutoff=0.88)
    return want[close[0]] if close else None


def parse(html: str, counties: list[str]) -> dict:
    """{county name: {name, title, address, phone}} from the list's table (County | [County] | Sheriff | Address |
    Phone Number; the page prints the county twice, once for small screens)."""
    from .extract import soup_of

    out = {}
    soup = soup_of(html)
    for tr in soup.find_all("tr"):
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip() for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        si = next((i for i, c in enumerate(cells) if TITLE.match(c)), None)
        if si is None or si == 0:
            continue                                     # the heading row, or a row with no sheriff
        county = county_key(cells[0], counties)
        if not county:
            continue
        name = TITLE.sub("", cells[si]).strip(" ,")
        name = name.replace("“", '"').replace("”", '"')
        if not re.search(r"[A-Za-z]{2,}\s+\S", name) or len(name) > 60:
            continue
        rest = cells[si + 1:]
        phone = next((PHONE.search(c).group(0) for c in rest if PHONE.search(c)), "")
        address = next((c for c in rest if not PHONE.fullmatch(c.strip()) and re.search(r"\d", c) and re.search(r"\bGA\b", c)), "")
        title = "Interim Sheriff" if re.match(r"\s*interim", cells[si], re.I) else "Acting Sheriff" if re.match(r"\s*acting", cells[si], re.I) else "Sheriff"
        out[county] = {"name": name, "title": title, "address": address, "phone": phone}
    return out


def _age_days(iso: str | None) -> float:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 86400
    except (AttributeError, ValueError):
        return 1e9


def _live(fetcher, log) -> tuple[str, str]:
    """(html, why it was not read) from the association's own site."""
    from .blocked import Blocked
    from .fetch import Disallowed, OutOfTime

    try:
        r = fetcher.get(URL, use_cache=False)
    except OutOfTime:
        raise
    except (Blocked, Disallowed) as exc:
        return "", f"refused ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}"
    if r.status != 200:
        return "", f"HTTP {r.status}"
    return r.text or "", ""


def _archived(fetcher, log) -> tuple[str, str, str]:
    """(html, capture day, the copy's address) of the newest capture within ARCHIVE_DAYS, or ("", "", "")."""
    from .wayback import RAW, SHOW, Wayback, day

    wb = Wayback(fetcher, log=log)
    since = (dt.date.today() - dt.timedelta(days=ARCHIVE_DAYS)).strftime("%Y%m%d")
    for cap in reversed(wb.captures(URL, newest=6)):
        if cap["timestamp"][:8] < since:
            break
        r = wb._get(RAW.format(ts=cap["timestamp"], url=cap["original"]))
        if r is not None and r.status == 200 and r.text:
            return r.text, day(cap["timestamp"]), SHOW.format(ts=cap["timestamp"], url=cap["original"])
        if wb.off:
            break
    return "", "", ""


def read(fetcher, root: Path, state_dir: Path, log=print, refresh: bool = False) -> dict:
    from .registry import jurisdictions

    p = state_dir / "sheriffs.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    if not refresh and cur.get("sheriffs") and _age_days(cur.get("as_of")) < REFRESH_DAYS:
        return {"counties": len(cur["sheriffs"]), "read": False}
    counties = sorted(j["name"].replace(" County", "") for j in jurisdictions(root) if j.get("kind") == "county")
    html, why = _live(fetcher, log)
    rows, via, captured, src = (parse(html, counties) if html else {}), "site", "", URL
    if len(rows) < MIN_COUNTIES:
        log(f"sheriffs: the association's list could not be read from its site ({why or f'{len(rows)} counties'}); "
            "trying the Internet Archive's copies")
        a_html, captured, src = _archived(fetcher, log)
        a_rows = parse(a_html, counties) if a_html else {}
        if len(a_rows) > len(rows):
            rows, via = a_rows, "wayback"
    if len(rows) < MIN_COUNTIES:
        log(f"sheriffs: only {len(rows)} counties read; the last list ({len(cur.get('sheriffs') or {})}) is kept")
        return {"counties": len(cur.get("sheriffs") or {}), "read": False, "found": len(rows)}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for c, row in rows.items():
        row.update({"src": src, "via": via, **({"captured": captured} if captured else {})})
    changed = sorted(c for c in rows if (cur.get("sheriffs") or {}).get(c, {}).get("name") not in (None, rows[c]["name"]))
    write_json_atomic(p, {"as_of": now, "source": URL, "site": SITE, "via": via, **({"captured": captured} if captured else {}),
                          "note": "Each county's sheriff as the Georgia Sheriffs' Association lists its members (159 elected "
                                  "sheriffs): name and title, the office's address and phone"
                                  + (", read from the Internet Archive's copy of the list (the site refused the crawler)" if via == "wayback" else "") + ".",
                          "sheriffs": dict(sorted(rows.items()))}, indent=1, ensure_ascii=False)
    missing = [c for c in counties if c not in rows]
    log(f"sheriffs: {len(rows)} of {len(counties)} counties from the {SITE}'s list"
        + (f" (Internet Archive copy of {captured})" if via == "wayback" else "")
        + (f"; changed since the last list: {', '.join(changed[:12])}" if changed else "")
        + (f"; not listed: {', '.join(missing[:12])}" if missing else ""))
    return {"counties": len(rows), "read": True, "via": via, "changed": changed, "missing": missing}
