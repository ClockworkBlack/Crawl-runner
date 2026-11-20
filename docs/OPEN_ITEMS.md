# Open items (saved Nov 19, 2026, after the full audit)

## 1. Waiting on runs
- Export-only run 44634387747 (started 23:04Z): imports the corrected councils and people into Electo. Then check:
  boards 202 -> about 84; no "Wellness" member in Fulton; the seven people lose House records that were not theirs;
  Floyd County's chief is not "Solid Waste"; grand-cross's data audit is green.
- ai-outlook: new cards under the seat rules (Electo #11); the "one short of a majority" card must be gone.
- Next full crawl: first on the Crawl #32 time budget; should reach its import unaided; Georgia money step keeps minutes.
- Ordinances backlog on the runner: `pending` in Electo's data/ordinances/index.json shrinks from about 685 files.

## 2. Authorities and transit (audit: 65 confirmed findings)
- GEFA: read upcoming-events and all past-events pages; agenda, place, time from each event page.
- GTEA (was ATL): reaches the site with the next import; add ATL/GRTA history; link each meeting's own agenda.
- Jekyll Island: accept Adobe-hosted agenda/minutes files; drop cancelled meetings (Dec 15).
- GHEFA: remove the GSFIC meeting shown as its own; read its four real meetings and notices.
- MARTA: each committee separately; 2025 page; official schedule; Audit Committee on Nov 20; drop cancelled meetings.
- Stone Mountain: current meetings with agendas; the board's minutes, not the committee's.
- Student Finance: each year's page; "Brief/Actions" summaries.
- Everywhere: meeting name, time and place; no cancelled meetings.
- Add: Georgia Ports Authority, Lottery Corporation, State Road and Tollway Authority, Technology Authority,
  Savannah-Georgia Convention Center Authority, Lake Lanier Islands, Building Authority, GPB.
- GWCCA: refuses automated readers; list under "not read" with the reason.
- North Georgia Mountains Authority: file its minutes under it, not the Board of Natural Resources.
- New "Regional Transit" bubble on the Meetings tab (owner's decision): MARTA moves there; add each regional
  transit authority that checks out against its enabling law (likely Chatham Area Transit, Macon-Bibb Transit).
- Esc closes the authorities drop-down after "Choose all" / "Clear".
- Watchdog: flag a body whose newest meeting is months old while its page has newer ones.

## 3. Loose ends from the audit
- Donors' spending: needs readers for FEC schedule B and the Ethics Commission's expenditures.
- 18 part-read sources are backed off; finishing them needs resumable passes in channels.py.
- Web search from the runner: check whether candidate research and county election reports find anything.
- FEC lines of amended filings can be re-dated into the wrong year (needs report_year in finance.py).
- City officials list is empty until the GMA pages are re-read (can be forced).
- By hand: add HB 803 and SB 333 (2023-24) to the bills list; remove stale election-night recipient notes.

## 4. Needs the owner
- Secrets on ClockworkBlack/Crawl-runner: YOUTUBE_API_KEY, LEGISCAN_API_KEY.
- Actions minutes: Settings -> Billing -> Usage (reset date, overage).
- Open records: close issue #27; Wadley's clerk and the misdirected letter; 6 held files (Tattnall, Flowery
  Branch); 10 late requests (#28); cost decisions (#6, #7, #8, #13); five portal-only places.
- Judgment calls to confirm: Jennifer Edwards Jordan lost Sen. Jen Jordan's record; namesake rumors rated "off"
  but still listed; small towns' roll calls stay with the council.

## 5. Pending questions for the owner (saved Nov 19, 2026; answer later)

### Email (open records mailbox)
- Wadley: tell the clerk (dflowers@wadleyga.gov) that a zoning review letter for a developer reached our request
  thread by mistake? Nothing has been sent. Issue #27 ("mentions a cost") can be closed: the $30 was that letter's.
- Wadley's Code was promised by Nov 16 and has not come: send a second follow-up?
- Four cost estimates wait for accept / narrow / decline: issues #6, #7, #8, #13.
- Ten requests are past their deadline after a follow-up (issue #28: Lowndes County, Elberton, Mount Airy, Boston,
  Clermont, Jeffersonville, Ray City, Alamo, Woolsey, Lexington): a third letter, a phone call, or leave them?
- Five places take requests only through a portal (Chatham County, Lithonia, Atlanta Public Schools, Richmond
  County Schools, Camden County): file by hand?
- Six files held for a look, not counted as the Code: Tattnall County's four resolutions, Flowery Branch's
  ordinances 788 and 789.
- Treutlen, Concord and Hazlehurst were wrongly marked fulfilled on a signature image: ask them again?

### City and county departments
- Transit run by a city or county department, not an authority (for example Athens-Clarke County Transit, Cobb
  County's CobbLinc, Gwinnett County's Ride Gwinnett, Augusta Transit, Columbus METRA, Albany Transit, Rome
  Transit): list them in the new "Regional Transit" bubble beside the authorities, in a bubble of their own
  ("City and county departments"), or not at all? Their decisions are made by the city council or county commission,
  whose meetings the site already lists.
- Other departments that hold their own public meetings (planning and zoning boards, boards of health, development
  authorities, water and sewer boards, housing authorities, airport and hospital authorities): which kinds should be
  read as bodies of their own, and under which bubble?
- Should a department's page of staff and contacts be read, or only bodies that meet in public?

### Judgment calls to confirm
- Jennifer Edwards Jordan (Pickens County clerk) lost Sen. Jen Jordan's Senate record: right?
- Rumors read as about a namesake are rated "off" and listed last, not hidden: hide them?
- Small towns' roll calls stay with the council even when few members can be identified: keep?
- MARTA and other regional transit authorities move to "Regional Transit" (decided Nov 19).

### LegiScan (Nov 20, 2026)
- `LEGISCAN_API_KEY` is set on Crawl-runner and reaches the job, but LegiScan answers GitHub's runners with a
  bot-protection challenge (api.legiscan.com and legiscan.com/robots.txt alike, probe run 44683847721). The crawler
  does not solve challenges or change how it names itself, so: ask LegiScan's API support to allow API calls from
  GitHub-hosted runners for this key (User-Agent `GACivicCrawler/0.3 (+https://github.com/ClockworkBlack/Crawl-runner)`).
  Until then roll calls come from legis.ga.gov only, with no LegiScan cross-check.
