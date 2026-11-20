# Strict format and tagging: the plan (saved Nov 20, 2026)

What this answers: records that reached the site filed as something they were not. Each was caught late, by the
audit or by eye, after the site had shown it.

| Seen | What was wrong | Kind of fault |
| --- | --- | --- |
| Nov 20 | "Governor - St. Marys", "US Senate - St. Marys": St. Marys' page held Camden County's whole report | a state office filed as a city's |
| Nov 20 | Enotah Circuit Superior Court seat "won" on Union County's votes alone, beside the state's result | a four-county race filed from one county |
| Nov 20 | Piedmont Circuit district attorney twice, once from Banks and once from Jackson | the same |
| Nov 20 | "Sandy Springs Mayor - South Fulton": South Fulton's page held Fulton County's runoffs | one city's race filed as another's |
| Nov 20 | The audit file the import committed listed 4 errors; 1 was real | a check run before its inputs were written |
| Nov 20 | grand-cross failed 11 times; the log digest never named the check that failed | an error without its cause |
| Nov 20 | The Georgia World Congress Center Authority missing from the site; Macon-Bibb Transit with no meetings | a saved copy of a challenge page taken for the Archive refusing: every archive read off since Nov 19 |
| Nov 19 | Boards named after an address or a date; two meetings of one day merged | a heading read as a record |

## The four rules

1. **One format per feed, written down, with a number.** A record carries exactly the format's keys, in its
   order. A file says which format it is in (`"format": 2`).
2. **The same tags on every record**, from fixed lists:
   - what level of government it belongs to (`lv`: federal, state, circuit, court, county, school, city, district,
     question, other; for a body `kind`: board, authority, transit)
   - which place it belongs to (`pl`, a place id) and who reported it (`jid`, `rep`: county or city)
   - where it was read (`u`) and when: published (`pub`), first seen (`seen`), changed (`upd`)
   - its status from the feed's list (a meeting: scheduled, held, cancelled, tentative)
3. **A gate at the import.** A rule of what is taken, not a list of what is not. What does not fit is left out
   under one reason from the feed's list, and counted. The ledger must add up: read = kept + left out.
4. **Proof after every import.** The audit checks the format, the tags and the ledger. An error fails
   grand-cross, and LogView files it under `data-audit` with the check named.

## Where each feed stands

| Feed | Format | Tags | Gate and ledger | Audit |
| --- | --- | --- | --- | --- |
| State boards, authorities, transit meetings | 1 (Nov 19) | kind, st, pub, seen, upd | gate; rows left out are counted | 9 checks |
| Reported election results (counties' and cities' pages) | 2 (Nov 20) | lv, pl, jid, rep | gate; ledger `ga/crawl/left_out.json` | 10 checks |
| Councils, boards, roll calls | none yet | place only | rules in the importer; no ledger | 8 checks |
| City and county meetings | none yet | place, date | rules in the importer; no ledger | none of its own |
| Policing (departments, chiefs, incidents) | none yet | place | rules in the importer; no ledger | 4 checks |
| Schools (districts, boards) | none yet | district | rules in the importer; no ledger | 2 checks |
| News, rumors | none yet | person key, date | rules in the importer; no ledger | 5 checks |
| Donors, links | none yet | person key | rules in the importer; no ledger | 5 checks |

Crawl's export (schema 2.3) already gives every record `published`, `first_seen`, `updated`, `fetched_at` and
`modified`; only the state meetings carry them through to the site so far.

## Done on Nov 20

- **Reported results, format 2** (Electo `ga/crawl_import.py`): every contest gets `lv` and `pl`, every report
  `jid` and `rep`. Who reports what:
  - federal, state and circuit offices (Superior Court judges, district attorneys): from no report. The state's
    results hold them whole.
  - a county's report: county, court, school, city, district.
  - a city's own page: its offices, its own school board, and seats it names alone. A contest that names
    another city in the same county is filed under that city, without the reporting city's name added.
  - a city's page that holds its county's report (a sheriff or a probate judge on it, or two or more state
    offices): only what its titles say outright is a city's.
  - a title the rule does not know is kept and listed by the audit, so the rule can learn it.
- **The ledger** (`ga/crawl/left_out.json`): for the results, what was read, kept and left out, by reason, with
  up to six examples each. On the Nov 20 bundle: 2,261 read, 222 kept, 2,039 left out (official results already
  hold it 1,434; ballot questions 457; state, federal or circuit office 131; an office the report's reader filed
  as a question 13; another office's candidates 4).
- **Audit**: 6 new checks (format, who reports what, tag against title, unknown titles, city offices that name
  no city, the ledger adds up). They start with the first import that writes format 2.
- **The Internet Archive is read again** (`gacrawl/blocked.py`, `wayback.py`, `agencies.py`): a saved copy
  that is its site's challenge page is passed over, never taken for the Archive refusing. A state body whose
  own site refuses and that has no reading of its own site on file is read from the Archive's newest existing
  copy, and says so. A body read only from the Archive with no copy yet is listed as not read yet.
- **The audit file is the true one** (`scripts/import_audit.sh`): the import runs the audit again after the
  name registry is written.
- **LogView**: a failed step's own error lines are kept, each failure gets a class, a cause and an issue id, a
  day lists one entry per cause, and each project has `ISSUES.md`: what is failing now. All 282 stored failures
  were read again under these rules.

## Next, in this order

Each step is one feed: write its format, add the tags, put its rules behind one gate with the ledger, add the
audit checks, test on the last real bundle before and after, and merge only when the difference is explained
record by record.

1. **Councils, boards and roll calls.** Format with `kind` (council, board, committee, authority), `pl`, and for
   each roll call its date, source and `seen`. Ledger reasons for a board named after a heading, a voter who is
   no person, a roll call with no date. This is where the Nov 19 faults were.
2. **City and county meetings.** The state meetings' format (name, day, time, place, status, agenda, minutes,
   summary, video, published, first seen, changed) for every local body, so the tab reads one shape.
3. **Policing and schools.** `pl` and `lv` on each department and district; ledger reasons for a chief or
   member who is no person.
4. **News and rumors.** `pub`, `seen` and the person key required; a story without a date or a link is left out
   and counted.
5. **Donors and links.** Amount, date, recipient key and source required.
6. **One table of formats** (`ga/formats.py` in Electo, mirrored in Crawl's `schema.md`): the keys, lists and
   reasons of every feed in one place, read by the importer, the audit and the tests.
7. **Circuits.** A table of each judicial circuit's counties, so a one-county circuit's race can be taken from
   its county's report when the state's results lack it.

## Open, found while doing this

- Clayton County's Nov. 7, 2023 report: three city council contests name no city ("City Council - Post 2").
  Crawl's report reader dropped the heading that named it. The audit now lists these.
- "Bradley Smith": one record holds a Conyers mayoral candidate (2025) and a McIntosh County commission candidate
  (2026). Likely two people. For the owner to confirm before they are split.
- 13 contests in counties' reports are filed as ballot questions though their titles are offices ("County BOE -
  Post 4 - Rep"): the report reader took a contest's rows for a question's. The ledger counts them apart
  (`misread-question`) so the reader's fault shows; the reader itself is next.
- Chatham Area Transit: its news page lists board notices through September only (three meetings). The reader
  matches the page; older and newer notices are on other pages it does not read yet.
- A body with readings of its own site on file whose site now refuses (the Board of Behavioral Health, since
  Nov 15) keeps those readings; it is not read from an older copy over them.
- LogView's failing list also shows hand-run jobs whose last run failed days ago (ai-compare-eval,
  archive-debug-apks, eval-stance, site-search). They leave the list when run again, or after 7 days.
