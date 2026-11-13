# Crawl — Georgia civic data crawler

Collects ordinances, council records (meetings, agenda items, roll-call votes),
school board records, city-run election results, ballot questions, campaign finance
and the organizations behind it, legislature records, candidate research (statements,
positions, articles, official-record mentions, votes, sponsorships, endorsements,
ratings, social posts, careers, connections) and a separate Rumor Mill of unverified
claims, for **every one of Georgia's 159 counties, 537 municipalities and 180 school
districts**, and hands it to the Electo site as import-ready JSON, per-person research
packets and profile supplements in Electo's own shapes.

## What's in it

| Piece | What it does |
|---|---|
| **Registry** (`config/jurisdictions.yaml`) | All 696 jurisdictions with Census 2020 FIPS codes, counties, population, county seat, consolidated-government flags. Code-of-ordinances links only where actually seen (never pattern-built). Hand edits in `config/overrides.yaml`. |
| **Seed** (`gacrawl seed`) | Finds each jurisdiction's website, then its meeting portal (Legistar, CivicClerk, CivicPlus AgendaCenter, Granicus, BoardDocs…), code library, feeds and official social accounts. A directory's listing is a candidate, not the answer: the hand override, Wikidata (by FIPS), the GMA city directory entry, the official page Electo opened and the Wikipedia infobox are tried in turn until one answers, and a dead `http://` listing is also tried as `https://` with and without `www.` (directories keep old spellings of sites that moved). A refusal is recorded with its reason (unreachable, robots.txt 403, bot protection: never worked around). Biggest jurisdictions first; time-budgeted; re-checks working entries older than N days, failed ones weekly, everything after a seed change (`SEED_VERSION`) and any entry whose override changed. `gacrawl seed --report` writes `data/seed_gaps.md`: what still has no working site, by cause, each with a ready `overrides.yaml` stanza. The GMA entry also gives the council's meeting day, officials (whole names, every seat), phone and city hall address. |
| **Database** (`data/crawl.db`) | SQLite (WAL) with full-text search. Upserts by natural key: a changed page updates its row (version+1) instead of piling up copies. Holds the URL frontier and resume cursors. |
| **Channel pick** | Per source: probe the official API, WordPress API, feeds, sitemap, HTML, browser; use the fastest working structured one. Probing fingerprints the platform first, so it costs 2–4 requests instead of a dozen blind guesses. Official websites also get a coverage crawl so a news feed never stands in for the whole site. |
| **Incremental crawling** | Re-runs ask APIs for changes only (Legistar `LastModifiedUtc`, WordPress `modified_after`), skip feed and sitemap entries older than the last run, never re-download a document (PDFs don't change), and revisit deep pages weekly. Links that look like agendas, minutes or ordinances are crawled first. |
| **Resumable backfill** | A first Legistar pull walks 2016 → today in yearly windows and checkpoints after each; an interrupted run resumes at the checkpoint instead of starting over. |
| **Scheduler** (`gacrawl run`) | Tiered cadence (counties and cities ≥10k daily, 1k–10k weekly, smaller monthly), most-overdue first, several hosts in parallel, inside a time budget. Page crawling only inside off-peak windows; APIs and feeds anytime. |
| **Parallel runs** (`--shard i/N`, `delta`, `merge`) | Sources split across runners by host, so each site is only ever hit from one place. Each runner ships a small delta; `merge` applies them (newest wins, idempotent). |
| **Politeness** | robots.txt (incl. Crawl-delay), per-host spacing with per-host floors (`http.host_intervals`), back-off on 429/503/slow responses, Retry-After, conditional GETs, honest User-Agent. |
| **Blocked sites** (`gacrawl blocked`, `gacrawl tasks`) | A host answering with a bot-protection challenge is left alone for a week; its sources re-route where possible; the rest becomes a manual task list with an Open Records Act request template. The crawler never tries to defeat bot protection. |
| **Electo bridge** (`gacrawl electo import`) | Imports every person on a Georgia ballot 2016–2026 from Electo (7,866 after folding alternate ballot names), ties local candidates to their city/county, seeds campaign sites and social accounts from existing profiles, flags split keys, and takes Electo's confirmed code-of-ordinances links. |
| **Candidate research** (`gacrawl candidates research`) | Mentions in every crawled official record (full-text search, no network); roll-call votes from the database (no network); news via GDELT with several names per query, each article **opened** and checked for the name **and** the place before it counts (only excerpts kept); Google News hits as leads; campaign-site statements and endorsements; verified social posts; Bluesky mentions; contributions (CSV import, FEC). Budgeted and resumable. |
| **Web lane** (`gacrawl/webresearch.py`, `candidates.web*` in settings) | What the news index's three-month window and the official records never reach (three of four profiles had nothing after the first full pass). Per candidate, people with nothing on file first: the **trusted outlets' own archives** (the WordPress news API of the county's papers, papers named for the place, and the statewide outlets for state offices; one phrase search per outlet back to 2016; the API answers with the story's text, so no page is opened) and **one web search** (DuckDuckGo through `ddgs`, no key; 300 fresh searches a run, answers kept 30 days) whose hits are opened only on sites already trusted: official sites and listed outlets. Anything else is left alone (not opened, not kept, not sent to the Rumor Mill). A page counts when it names the person and their place, or, on the place's own paper or website, their office beside the name. Kept as `article` records (`meta.via`: `outlet_archive` or `web_search`); progress per candidate (`web:<id>`), again after 30 days; a search that could not run is owed the next day. |
| **Evidence** | `primary` (official record / candidate's own channel), `reported` (listed outlet), `lead` (found but not opened: never cited), `unverified` → Rumor Mill only. Ballotpedia and AI summary portals are excluded. |
| **Export** (`gacrawl export`) | `dist/electo/`: jurisdictions, per-jurisdiction records, council vote-alignment tables, candidates, **research packets** in Electo's `{t,u,d}` source format, verified posts, rumor mill, blocked hosts, manifest, schema. Stable ids for upserts. |
| **Screenshots box** (`gacrawl screens`) | Drop screenshots in `imports/screens/` (`ordinances/`, `posts/`, `text/`, or the top folder to auto-sort), from a phone via github.com upload. Each is read and filed in the Screenshots box: duplicates skipped, phone status/navigation bars removed, dark mode handled, long scrolls tiled and consecutive shots merged, platform/handle/dates/engagement/URLs picked out, places and candidates matched. Items stay *needs review* until the original is opened. `screens capture URL` screenshots allowed pages as a phone or desktop (never sites that refuse crawlers); `screens simulate` makes fictional test screenshots. |
| **Downloaded ordinances** (`gacrawl ordinances`, `imports/ordinances/`) | Codes of ordinances, zoning codes, charters, ordinances and resolutions downloaded by hand go in `imports/ordinances/<place>/`, or the downloader's whole `GA/<county>/code of ordinances/{code.html,toc.json,manifest.json,nodes/}` tree (or zips of it) is dropped as is: each such folder is one code, placed by the first folder on its path that names a government, a bare shared name going to the county (registry id or name; `<id>__file` or a lone place named in the text also work; optional `sources.csv` gives each file's URL, title, date, kind). PDF (OCR for scanned pages), Word, saved web pages, text and images are read into sections at their own headings (Sec. 2-14, ARTICLE IV ...) and sent to Electo's `data/ordinances/` (`index.json`, `docs/<id>.json`, `schema.md`) by the private-only `ordinances` workflow on every upload. Files are never moved; unchanged files are not read again. Never copied to the public runner. |
| **Screenshot → text** (`gacrawl ocr`) | Screenshots, scans and PDFs to Markdown with ordinance-aware headings, confidence scores and lines to double-check. |
| **CivicClerk portals** (`gacrawl/sources/civicclerk.py`) | The public OData feed behind 17 governments' portals (Macon-Bibb, Henry, Coweta, Carroll, Banks, Butts, Charlton, Grady, Greene and Lumpkin counties; Alpharetta, Forest Park, Hapeville, Kingsland, Norcross, Villa Rica, Waycross). The server pages 15 events at a time whatever is asked for, so the reader pages by the server's own count (Kingsland: 577 events; the old reader stopped at the first 15), walks a first pass in year windows with a checkpoint after each (an interrupted pass resumes there), then re-reads the last 45 days and anything created since the last run (minutes arrive weeks after a meeting). Agendas, packets and minutes come embedded with each event and are opened once. Meeting locations carry the city, state and ZIP the API gives. |
| **Reader upgrades** (`READER_VERSIONS` in `gacrawl/scheduler.py`) | When a reader starts reading deeper (CivicClerk from 15 meetings to every meeting since 2016; YouTube from 200 videos to the whole channel, listed page by page with the page token as a checkpoint), a source the older reader marked done is due at once and read from the start, whatever its cadence; the version is stamped on its progress when the new reader completes it. |
| **Purge** (`config/purge.yaml`, `gacrawl purge`) | Sources removed for cause, with the reason: their records, the research mentions that cited them, their cursors and frontier rows are deleted by the merge job before deriving (the 43,221 Columbus, Ohio Legistar records that made Electo quarantine Columbus, Georgia). |
| **Derive** (`gacrawl derive`) | Parses what was crawled into structured records, incrementally: city/county **election results** and qualifying lists (Electo archive shape, `elections.json`); **ballot questions** (SPLOST, ESPLOST, homestead, bonds, alcohol, charter, annexation…: text, amounts, results, `ballot_questions.json`); **minutes → motions and named votes** (movers, seconders, ayes/nays, roll calls, tallies, unanimous; every vote carries its basis); **press releases → appointments** (who was appointed to what, by whom, term); **endorsements, scorecards and questionnaires** from organizations' own pages (`config/guides.yaml`). |
| **Transcripts** (`gacrawl transcripts`) | Meeting video pages → captions/transcripts (Granicus viewer, VTT/SRT, YouTube Data API; optional local Whisper). Candidate mentions carry a time code; a candidate's own speaker-labeled lines become positions (marked needs_review). |
| **School boards** (`config/school_districts.yaml`) | All 180 districts (159 county, 21 city) in the registry; BoardDocs adapter (meetings, agenda items with permalinks, opened attachments — minutes feed the vote parser); district websites. |
| **Schools** (`gacrawl schools`, `config/schools.yaml`) | Every public school, private school and college in Georgia with coordinates, from NCES's EDGE data services (Common Core of Data 2024-25, Private School Survey, IPEDS), each tied to one of the 180 districts, plus every district's outline (so an address finds its district). Official statistics by district and school from GOSA's downloadable data (graduation rate, enrollment, attendance, dropouts, certified personnel, retained students, HOPE eligibility, completers, financial efficiency and per-pupil spending, ACT) and GaDOE's CCRPI files, newest year of each series, found on the index pages themselves. Board policy manuals and meetings on GSBA's eBOARD/Simbli (`<id>:policies`, browser channel; a policy page carries its GSBA code and adoption date). **Board members, superintendents and principals** read off the districts' board pages and the schools' own pages (school sites are the links the district's "our schools" page gives; each district's superintendent and board pages are followed from its own home page once a month, `schools:district_pages`); each becomes a research candidate with the same packet and profile supplement as everyone on a ballot (official-record mentions, board roll calls from minutes, news, statements), and one who also ran for office keeps the Electo key. Board metrics are counts from the minutes (meetings, recorded votes, unanimous and divided votes, each member's recorded votes), never characterizations. Exported as `schools.json`, `schools/<district>.json` and `school_bounds.json`. |
| **Legislature** (`gacrawl legis`) | legis.ga.gov's own data service (the calls Electo makes): members and committees, every bill with sponsors, roll calls for landmark bills (`config/legislature.yaml`), passed legislation and then every bill of every followed session whose record shows a floor vote (`votes.all_floor_votes`; newest session first, 400 bills a run on the public runner, a closed session's bill once); per-candidate votes/bills/terms/committees in Electo's profile shapes. Current sessions re-listed every run; history once. |
| **Vote model** (`gacrawl legis votemodel`, `vote_model.py`, `vote_model.json`) | How each legislator votes, added up. From every member's Yea/Nay on the stored roll calls (no requests): a left-right position per member fitted to the roll calls that divided their chamber; the chance each member votes Yea on a roll call (their position, party and record, the bill's sponsors and committee, how each party and the member voted on the bill's earlier roll calls); and each roll call's expected Yea count, the sum of those chances. Scored forward in time only (a year's votes from the years before it) next to plain rules on the same questions (always Yea, the member's usual vote, the lead sponsor's party, their party on the bill's earlier vote), with a verdict: the file says so when the model is no better than a rule. Built at export when numpy, scipy and scikit-learn are installed (the crawl workflow installs them); positions only until 60 divided roll calls are in the store. |
| **Legislature votes, night and day** (`.github/workflows/legis-votes.yml`, `scripts/legis_handoff.sh`, `gacrawl/legis_handoff.py`) | The roll calls of every bill since 2015 that reached a floor vote, LegiScan's datasets first (every roll call of a session in one download, so the vote model has the whole history within a night), then legis.ga.gov for up to 2.5 hours a night and three times by day on the public runner (free minutes), at the site's own pace (2 s between requests at night, 4 s by day), newest session first; past bills LegiScan already gave in full come last, where the site's reading is the cross-check. An unreachable site or a refused token is waited out (15 minutes, then again while time is left). It works on a copy of the crawl database and hands what it read to the crawl's next merge (`legis-delta.db.zst`, `legis-kv.json` in the private crawl-data release; applied, then deleted once stored, so it never races the crawl's upload). With nothing left to read it ends in minutes. `gacrawl legis waiting` says how many bills are left. |
| **Roll-call check** (`check_roll_call` in `sources/legis.py`) | Every member-by-member list is checked against the tally the site gives beside it: Yea and Nay counted from the list equal the reported ones, excused and not voting together equal the reported ones, nobody listed twice, no more members than the chamber's seats (180 / 56), the voters on the session's member list. A list that fails is kept but marked (`meta.check`), read again on the next two runs, and never used by the vote model; LegiScan's copy is used in its place. |
| **LegiScan** (`gacrawl legis legiscan`, `sources/legiscan.py`) | The backup source for roll calls: LegiScan's weekly Georgia datasets (every bill, roll call and legislator since 2015) through its API with a free key (`LEGISCAN_API_KEY` secret on the runner). One call a night; a dataset is downloaded only when its hash changed. Legislators are matched to legis.ga.gov members by chamber, district and last name; bills by their legis.ga.gov link. A roll call legis.ga.gov did not give, or gave with a list that failed the check, is taken from LegiScan; every roll call both have is compared, tally and member by member (`vote_model.json` `sources`: agreement shares and each disagreement). The key is never stored or logged (errors are redacted). LegiScan's data is CC BY 4.0: the bundle credits it for every roll call taken from it. |
| **Official YouTube channels only** (`transcripts.official_channel`, `rumor_video.py`) | A meeting channel becomes a source only when the government points to it (a link on its official website found by the seed, or a hand check in `config/overrides.yaml` as `youtube_checked`) and the channel is plainly the government's (it names the place or reads as a government channel; nothing marking a chamber, news outlet, station, party or residents' group). Videos from any other channel go to **Rumor Mill - Video** (`data/rumor_mill/videos.jsonl`, exported as `rumor_mill_video.json`), never into the records; to promote one, add it to `config/channel_decisions.yaml` (status `confirmed` or `rejected`; the owner's decisions win over every name check, and a confirmed channel's videos leave the Rumor Mill export and become records). The open model (`gacrawl channel-ai`, `channel_ai.py`) judges every channel still unverified against those decisions: it confirms one on its own only when it is as good as the owner's (the official website links it, every video is a meeting of that body, and the model says so; kept in `state/channel_ai.json`), else lists it in `data/channel_review.md` with the lines to paste. Every decision is a labeled example in `data/train/channel_log.jsonl`. Candidate channels come only from verified accounts (linked from the candidate's official pages). |
| **Meeting videos read** (`gacrawl video-ai`, `video_ai.py`) | Official channels' meeting videos: captions through yt-dlp (no key; the video's own captions, else YouTube's automatic English ones) become `transcript` records with time markers; the open model lists what each meeting did (motions, votes, agenda items, statements; every line copied from the transcript, statements marked for review) and writes a checked two-to-three-sentence summary (`meta.ai`). If YouTube asks the runner to sign in or prove it is not a bot, the step stops for the day; nothing is worked around. Districts' `video_list` (schools export) carries each video's reading. |
| **The Rumor Mill as research leads** (`discover_ai.py` `rumor_leads`) | Rumor items the open model read and found to name the person (rumors_ai.py) are searched for in trusted outlets and on official pages; a page is kept only when it is one of those, names the person and tells the same story, and is then noted on the rumor (`research`). The rumor page itself is never a source. |
| **Throughput** (`candidates.workers`, `source_slice_minutes`, `gacrawl summary`) | Candidate research runs several candidates at once (6 by default), in chunks of up to six from one place in priority order. News has its own lane beside them: one GDELT search at a time for up to six names from one place (GDELT can take a minute to answer, and refuses a query sent the moment the last one answered, so its spacing counts from the answer and its timeout is 120 s), the hits opened four at a time, with its own per-candidate progress (`news:<id>`; every 14 days, 3 for recent races). Google News refuses crawlers in its robots.txt, so it gives no leads; a candidate's own `news_feeds` are still read. Campaign sites are read once (the way in is kept 14 days), 25 pages with issue, about and endorsement pages first; dead sites are left alone 30 days. Sources are ordered by what a short run should reach first (resume, overdue, jurisdictions with candidates, tier, structured channels before page walks) and one source gets at most a sixth of the run; it resumes next time. PDFs are read with PDFium (6–20× faster than before); OCR gets 90 s per document during a crawl and the nightly `gacrawl ocr pending` step finishes the rest. Budgets hold: every budgeted command hands its deadline to the fetcher (a wait for a host's turn that would end after it is not taken), the crawl and research steps have a watchdog that sends an interrupt after the budget and both finish what is in hand and save, a candidate the deadline cut short stays due, and derive keeps its place when it runs out of time. Every run writes a Markdown summary to the job's Summary tab. |
| **Daytime backfill** (`config/backfill.json`, `gacrawl backfill status`) | Back-to-back runs from 6 AM to 9:30 PM ET (one API-only crawl shard, 100 minutes of candidate research, 20 of finance), each dispatching the next, until every candidate has been researched once and two runs in a row add fewer than 60 records; then it switches itself off. While it is on, the nightly run uses 3 shards × 40 minutes inside the off-peak window (council minutes and roll calls). Each run counts the month's Actions minutes from the Actions API (every run of every workflow and branch in this repository: test runs, probes, cancelled runs; skipped jobs and self-hosted runners cost nothing) and keeps the count in `state/backfill.json`; the chain pauses at `month_minutes_cap` (4,500, sized for the $30 budget). Runs in other repositories (Electo's geo lookup) are not counted. Set `enabled` to false to stop it. |
| **Code changes of enacted acts** (`gacrawl codeacts`, `code_acts.json`) | Every enacted 2023–2026 act's as-passed text (legis.ga.gov document service, the "/AP" version), read character by character with the PDF's drawing layer so struck-through (removed) and underlined (added) words are known. Each act section becomes exact operations: the target Code section and unit ("paragraph (4) of subsection (a)"), the quoted Code text as enacted and as it read before, Code Revision word edits ("in paragraph (a)(1), by replacing "is when" with "means""), repeals, new sections, and per-section effective dates (including acts that move another act's date, like SB 3EX moving SB 189 § 7 to 2028). Read once per act. Electo applies them to its Release 86 text, checking each change's "before" text against the text it amends. Also `legislation.json`: every 2023–2026 bill in Electo's `acts.json` shape. |
| **Campaign finance** (`gacrawl finance`) | Commission CSV exports, **CCDR PDFs posted by cities** (parsed; needs_review), FEC receipts and disbursements; **entity resolution** across filings (ids kept in the database so they stay stable run to run); **what each organization is** (FEC committee details, IRS 990 data via ProPublica, OpenCorporates with a key, the org's own about page, hand notes in `config/organizations.yaml`), who runs it (treasurers, officers), who it funds. The Commission's search is used only once a person confirms its URL (`gacrawl finance probe` lists what its front page offers). |
| **Connections** (`connections.json`) | Graph of donors, organizations, candidates, offices, sponsorships, endorsements, appointments with the records each edge rests on; **patterns** (shared donors, employer clusters, org→org→candidate chains, officer overlap, donors who hold office, shared endorsers) and **donor-matter leads** (a donor's name in a matter the recipient voted on, evidence `lead`: verify first). |
| **Profile supplements** (`gacrawl profiles`, `profiles/<key>.json`) | Everything on a candidate in the item shapes Electo's inference reads: ideology line composed from documented markers only (party, self-descriptions, caucuses, ratings, endorsements), positions by issue (`config/issues.yaml`) from own channels, transcripts, listed outlets and questionnaires, statements, affiliations, endorsements (+esrc), ratings, votes (council and legislature), bills, ordinances supported/opposed, a sourced career timeline, money. `inference_inputs` counts what Electo would see; `profiles/index.json` lists the thinnest first. |

## Sourcing rules (shared with Electo)

- Every record's `url` is a page or API resource the crawler actually opened (HTTP 200), or a link that resource
  itself returned. Nothing is built from a URL pattern.
- Articles count only after the crawler opened them and found the candidate's name and place in the text; same-named
  people elsewhere are dropped. Search hits that can't be opened are `lead`s, listed so a researcher can open them.
- Unverified material never reaches a profile or packet's sources; it sits in the Rumor Mill with a status
  (unverified → corroborated / debunked / retracted) and a corroboration trail.
- Packet `vote_topics` are counts of recorded votes, not stated positions.
- The Wayback Machine is read only for what is gone or no longer theirs (`gacrawl/wayback.py`): a campaign site
  that answers 404 / no such host, or whose domain lapsed and now shows a casino, a for-sale page or a page in
  another language, is read from its last capture that still named the candidate (its pages as captured up to that
  day; records link the archived copy, `meta.original` is the old address, the profile's `site.archived` says when
  and why); an article that answers 404/410 is read from its newest capture (`meta.archived_url`). A page that
  answers but is closed (a paywall, a login, 401/402/403, a challenge page) is never read through an archive. One
  request every 4 s, no retry after a 429, and the first 429 or refusal switches it off for the run
  (`candidates.archive`, `archive_sites_per_run`, `archive_articles_per_run`).

## Quick start

```bash
pip install -e .
sudo apt-get install tesseract-ocr        # OCR
python -m playwright install chromium     # only for script-rendered sites

gacrawl electo import --path ../electo    # people, councils, profile links, code links
gacrawl seed --max-minutes 30             # discover sites/portals (resumable)
gacrawl run --tier a --max-minutes 30     # crawl the big jurisdictions
gacrawl derive --max-minutes 10           # results, ballot questions, minutes votes, appointments, guides
gacrawl transcripts --max-minutes 10      # captions for meetings with video
gacrawl sponsors                          # Legistar sponsor lists
gacrawl legis --max-minutes 15            # General Assembly members, bills, roll calls
gacrawl schools all --max-minutes 15      # schools, colleges, district outlines, statistics, school sites, people
gacrawl candidates research --max-minutes 20
gacrawl finance --max-minutes 20          # money, entities, what the organizations are
gacrawl profiles                          # profile supplements (also written by export)
gacrawl search "short-term rental" --jurisdiction canton
gacrawl export                            # dist/electo/
gacrawl status                            # coverage, crawl progress, database size
```

Useful flags: `run --source co-fulton` (one jurisdiction), `--force` (ignore the off-peak window),
`--full` (re-crawl history instead of a delta), `--shard 0/4`.

## Candidates

All Electo people are candidates once imported; narrow with `electo.levels` / `electo.min_last_race_year` in
`config/settings.yaml`. Add or enrich anyone in `config/candidates.yaml` (by `electo_key` or a new `id`):

```yaml
candidates:
  - electo_key: "JANE DOE"
    websites: ["https://janedoeforcanton.com"]   # first = proof page for social accounts
    social: ["https://bsky.app/profile/janedoe.bsky.social"]
    finance_csv: imports/finance/jane-doe.csv    # GA Campaign Finance Commission export
```

```bash
gacrawl rumor list --candidate jane-doe
gacrawl rumor corroborate --id r00001 --evidence https://www.ajc.com/... --note "confirmed by AJC"
```

GDELT's news index covers a rolling ~3 months, so news coverage builds up week by week rather than reaching back
to 2016; older coverage comes from official records, campaign sites and leads.

## Scheduling and cost

`.github/workflows/crawl.yml` runs **prepare → crawl (N shards) → merge** nightly at 04:07 UTC, with seeding and
candidate research added on Sundays. The database and the Electo bundle are assets of the `crawl-data` release;
small state files are committed here.

Private repos have a monthly Actions allowance, counted in dollars (about 2,000 Linux minutes on GitHub Free, 3,000
on Pro, at $0.006 a minute past that). Every step has a time cap rather than a fixed cost: nightly, two 18-minute
crawl shards plus derive (5), transcripts (4), Legistar sponsors (3), candidate research (13), the acts' Code
changes (up to 45 while their backlog lasts) and, January–May, the legislature (6); weekly, seed (20), candidate
research (90), campaign finance (15) and the legislature (6). About 2,400 minutes a month on the nightly schedule;
the daytime backfill (`config/backfill.json`) adds about 1,100–1,250 a day while it runs and stops itself when
profiles stop filling. Raise throughput with repository variables `CRAWL_SHARDS`, `CRAWL_MINUTES`, `SEED_MINUTES`,
`RESEARCH_MINUTES`, or use a self-hosted runner (no minute cost; `CRAWL_RUNNER` / `MERGE_RUNNER` under **Coverage**).

Secrets are listed under **Secrets and API keys** below.
Minutes per step: `DERIVE_MINUTES`, `TRANSCRIPT_MINUTES`, `SPONSOR_MINUTES`, `LEGIS_MINUTES`, `FINANCE_MINUTES`,
`SCHOOLS_MINUTES` (repository variables).

## Developing (the `dev` branch)

Work on `dev` (or any branch) and merge to `main` with a pull request. Only `main` runs on a schedule. A run started
from another branch (Actions → crawl → Run workflow → pick the branch) is a test: it starts from a copy of the live
database, writes only to the `crawl-data-dev` release, never commits state, never pushes to Electo, never chains
backfill runs, and runs one 8-minute crawl shard (`DEV_CRAWL_MINUTES`) with 10 minutes of research and every other
step capped at a few minutes (about 45 billable minutes in all). It has its own concurrency group, so it never waits
behind the live crawl. Leave `force` off to see the daytime behaviour (page sources wait for the off-peak window);
turn it on to make the shard read pages now.

## Secrets and API keys

Every key is a repository secret: GitHub stores it encrypted and the workflow passes it to the steps that need
it; it is never written to a file, a log or the database. Keys go in by hand, never through a chat or a commit.

**Adding a secret.** github.com/ClockworkBlack/Crawl → **Settings → Secrets and variables → Actions → New
repository secret**: type the name exactly as below, paste the value, **Add secret**. Or, from a terminal with the
GitHub CLI, `gh secret set FEC_API_KEY --repo ClockworkBlack/Crawl` (it prompts for the value, which stays out of
your shell history). The next run uses it; nothing else changes. Each run's research step prints which keys are
set or missing (never their values).

| Secret | What it adds | How to get it |
|---|---|---|
| `FEC_API_KEY` | Federal campaign finance (FEC receipts, disbursements, committees) for candidates and donors. Without it the public DEMO_KEY allows 30 requests an hour, too few to matter. | **api.data.gov/signup** (needs a browser with JavaScript): name and email; the key arrives by email (click the verification link if asked). 1,000 requests an hour. Free. |
| `YOUTUBE_API_KEY` | Candidate videos and council meeting channels (listings and transcripts) for profiles and Grand Cross. | **console.cloud.google.com**: create a project (any name) → **APIs & Services → Library** → *YouTube Data API v3* → **Enable** → **APIs & Services → Credentials → Create credentials → API key** → open the key, **API restrictions → Restrict key** → only *YouTube Data API v3* → **Save**. Free: 10,000 units a day; the crawler uses 1-unit calls. |
| `ELECTO_TOKEN` (optional) | Not needed for the normal path: Electo's build downloads the `electo-bundle.tar.gz` asset from the `crawl-data` release itself, and the candidate list was imported once with `gacrawl electo import` (re-run it when people are added in Electo). With the token, each run also re-imports Electo's current people, councils and links first, and pushes packets, profiles, connections, elections and ballot questions into Electo's `data/crawl/` after. | **github.com/settings/personal-access-tokens → Generate new token** (fine-grained): **Only select repositories** → *ClockworkBlack/Electo*; **Repository permissions → Contents: Read and write**; set an expiration and renew it before then. |
| `X_BEARER_TOKEN` | Verified candidate posts on X. | **developer.x.com**: an X developer account and app; the app's Bearer Token. Check the current API plan terms first. Optional. |
| `OPENCORPORATES_API_KEY` | Georgia corporate officers for the organizations in campaign finance. | Apply for an API account at **opencorporates.com**. Optional; without it officers are looked up by hand (`config/organizations.yaml`). |
| `ORR_SMTP_HOST`, `ORR_SMTP_PORT`, `ORR_USER`, `ORR_PASSWORD`, `ORR_FROM`, `ORR_IMAP_HOST` | Open records requests from the project mailbox (see **Open records requests**). | The mailbox provider's SMTP/IMAP settings; use an app password, not the account password. |

**Spending cap (before a backfill).** github.com/settings/billing → **Budgets and alerts → New budget** →
**Product-level budget**, product **Actions**, scope the whole account (or this repository) → amount (for example
$30) → check **Stop usage when budget limit is reached** and **Receive budget threshold alerts** (75%, 90%,
100%) → **Create budget**. Keep one Actions budget only: overlapping budgets can block each other. GitHub takes
credit cards and PayPal, not prepaid cards; a merchant-locked virtual card (from your bank or Privacy.com) with
a monthly limit adds a second cap. `config/backfill.json`'s `month_minutes_cap` is the crawler's own limit.

## Performance (`python scripts/bench.py`, local server, no politeness delay)

| | before | after |
|---|---|---|
| Re-crawl of a 300-page site with 150 PDFs | 451 requests | 31 requests, 0 PDFs re-downloaded |
| Same re-crawl at the real 3 s/request spacing | ~22 min | ~1.5 min |
| Find 50 candidates' names in 100,000 records | 21.5 s (scan) | 0.085 s (full-text index), identical hits |
| Database writes | — | ~6,500 records/s |

Nightly pipeline on a large synthetic database (`python scripts/bench_pipeline.py`: 268,000 records, the real 7,866
candidates, 30 councils with 3,000 roll calls each, 60,000 contributions, 1,800 transcripts):

| step | before the audit | after |
|---|---|---|
| Research, votes and sponsorships (400 candidates) | 30.5 s | 14.4 s |
| Entity resolution over 60,000 contributions | 76.5 s | 6.8 s |
| Connection patterns (127,000 edges) | 24.3 s | 1.8 s |
| Money summaries (300 candidates) | 73.6 s | 0.16 s |
| Profile supplements (7,866 candidates) | 481.6 s | 18.4 s |
| Export (full bundle) | 521.0 s | 32.9 s |

What changed: per-candidate scans became shared indexes built once per run (roll calls and sponsor lists per
jurisdiction, transcript speakers, office records, minutes attendance, appointments, graph adjacency, pattern
membership, a token index for donor names in votes); updates that only touch `meta` no longer re-index full text;
name normalization is memoized; YAML is parsed once with libyaml; the export reads each candidate's records once
for its file, profile and packet, and streams jurisdiction files without re-encoding stored JSON. Per-candidate files
carry no run timestamps, so unchanged data produces byte-identical files and Electo's repository only sees real
changes.

Network: Legistar roll calls come from each office holder's vote history (one paged call per person) instead of one
call per agenda item: a 600-meeting backfill drops from about 6,600 requests to about 670 with identical roll calls,
and a nightly delta from roughly 90 requests per Legistar city to about 12 (changed votes are merged into stored roll
calls; recent agendas are re-read weekly instead of nightly). The FEC is read with its seek pagination and a
per-committee cursor; candidate ids and organization lookups are cached; the same page asked for twice in one run is
fetched once; 16 workers crawl 16 different sites at a time (per-site spacing unchanged); scanned PDFs are OCR'd up to
60 pages per document. CI: the runner's own Chrome replaces Playwright's browser download, the database moves as
zstd, the full-text index is fully optimized weekly (a bounded merge other nights), and large state lives in the
database instead of files committed every night.

## Donors (`donors.json`)

For Electo's Donors tab: every contribution and expenditure in the store (FEC itemized receipts and disbursements for
federal candidates, Georgia CCDR filings read or imported), aggregated per recipient (raised, spent, in-Georgia and
small-gift shares, top employers and payees), per donor → recipient (largest first, capped), per payee, and donors
who gave to more than one recipient, plus totals by donor state, county, city, recipient party, office and level,
donor kind and filing source. Built at export from `gacrawl finance`'s records; see `gacrawl/donors.py`.

## Why bills passed or died (`gacrawl legis reasons`)

For Electo's Bills tab, its AI and its Pass Likely / Pass Not Likely estimates. Two kinds of evidence, kept apart.
**The official record** (no requests; built at export from `gacrawl legis update`): each decided bill's outcome (law,
vetoed, died), how far it got, every floor vote with the tally by party (party-line or bipartisan), the lead
sponsor's party and whether sponsors came from both parties, and its committees. **What was reported**: for each
decided bill (signed and vetoed first, then the ones that died; key bills first), each outlet in
`config/outlets.yaml` that advertises a WordPress news API on its own home page is searched for the bill's number
within the session's dates; an article is kept only when it names the bill, and only the sentences around the number
that give a reason (supporters' and opponents' arguments, a veto, a missed deadline, a committee that never voted, a
cost) are stored, tagged support / oppose / outcome. A closed session's bill is looked up once; a current one again
when it moves. **Patterns**: how often decided bills with each feature became law, across every session in the store.
Every crawl run's merge job gives it 15 minutes (`BILL_REASONS_MINUTES`). State: `state/wp_outlets.json`. Export:
`bill_reasons.json`.

## Policing contacts and media tip lines (`gacrawl police`)

For Electo's Policing tab. `gacrawl police` follows each seeded county's and city's **own link** to its police
department, sheriff's office or public-safety department (same site, or another host the home page itself links to
that names the place), then up to four of that section's pages (chief / command staff, contact, precincts or zones,
internal affairs or complaints, records). A county keeps its sheriff's office and its county police separately (the
second under `also` in `state/police.json`); a city keeps its police department, or its marshal. When the home page
links only to a department's services ("File Police Report"), the government's own Departments page is read for the
landing pages; when its menus are built by scripts (cobbcounty.gov), the sitemaps its robots.txt names are. It keeps,
each with the page it was read on: the department's name, the chief or sheriff with the title as printed, phone numbers
with the label beside them, emails on the government's domain, the street address, precincts with their addresses, and
the complaint and records pages (on a county's site, only the department's own links, never the site-wide "Contact
Us"). Nothing is guessed (no pattern-built URLs or emails, no inferred chiefs); a missing field was not printed on the
pages read. A department is re-read after 30 days, a place where none was found after 7, and every place when
`POLICE_VERSION` changes (`--refresh` reads all now).

`gacrawl police roster` reads the FBI's list of every Georgia law enforcement agency (the Crime Data Explorer API,
api.usa.gov/crime/fbi/cde; once a week, `state/police_roster.json`). It needs an api.data.gov key: `DATA_GOV_API_KEY`,
else the project's `FEC_API_KEY` (api.data.gov issues it, and one key works for every API it serves), else the public
DEMO_KEY. The export adds every county's and city's department on that list that the pages did not give (name and ORI,
`listed_by: "fbi"`), and lists each county's other agencies (campus, school-system, transit and airport police,
marshals, the State Patrol post) as `others`. `gacrawl police media` reads each outlet in `config/outlets.yaml` for the
news-tip addresses on its own contact page (re-read after 30 days). State: `state/police.json`, `state/media.json`,
`state/police_roster.json`. Exported as `police.json`, `media.json` and `records_contacts.json` (the open-records
contacts the seed found, see below) in the Electo bundle. These are read by their own workflow, `police.yml`
(concurrency group `police`, so it never queues behind the crawl): three runs a weeknight and one more on weekend
nights, only inside the off-peak window; it commits the state files and patches those three files into the published
bundle (`gacrawl export --policing-only`), so Electo can import them before the nightly export. Once every department
is current a run ends in a minute or two. Actions -> police -> Run workflow runs one now (outside the window it reads
nothing). Electo's import names every county's sheriff from the Secretary of State's results, so the Policing tab shows
all 159 even where neither the pages nor the FBI's list has the office.

`gacrawl police news` then asks GDELT once a week per department (those read from their pages and those on the FBI's
list alike) for news about it and its chief, keeps only hits from the outlets in `config/outlets.yaml` or official
pages, opens each, and keeps the ones that name the department and describe an incident (shooting, use of force,
lawsuit or settlement, firing or suspension, indictment, misconduct complaint, investigation, pursuit, in-custody
death). Each `police_incident` record carries the excerpt around the first cue and the people the article names **with
a rank** (Officer, Sgt., Chief, Deputy...); the export groups those mentions into per-department profiles
(`police_incidents.json`, one entry per department, a county's sheriff and police apart): what was reported, by whom,
with every line linked to the article. It runs early in every crawl run's merge job (it owns the database), up to 15
minutes, with its progress in `state/police_news.json`. `config/outlets.yaml` also says where each outlet reports from
(`statewide`, `regions` with an anchor point, `county_outlets`, `names`) for Electo's "email the news" picker.

## Open records requests (`gacrawl records`)

For jurisdictions whose code can't be crawled, the crawler asks the clerk for it under the Georgia Open Records
Act from a dedicated project mailbox: it finds the records email or portal on the official site (`records
contacts`; never guessed addresses), drafts one request per jurisdiction signed with `openrecords.requester`,
sends a small daily batch (`openrecords.daily_limit`, default 10), never asks the same place twice within
`cooldown_days`, reads replies (attached codes go to `imports/screens/ordinances/` and into the Screenshots box;
cost estimates, denials and bounces are recorded), and sends one follow-up when the 3-business-day deadline has
passed. Portal-only jurisdictions are listed for submitting by hand. The tracker is `data/openrecords/requests.json`.

Only cities, towns, counties and consolidated governments are asked (school districts have board policies, not
ordinances). Addresses are vetted: a clerk or open-records address on the place's own domain is used; police,
sheriff, fire, 911, health, court, newsroom, HR, finance and similar department addresses, other domains, and
test or no-reply addresses are dropped; a portal is used when there is no clerk address (never a sheriff's portal
or a link that is really an image or PDF). Places with only a person's own address or a general inbox are listed in
`data/openrecords/review.md`; approve an address (or skip a place) in `config/private/openrecords.yaml` (private repository only).

**Test first:** Actions → records-test → Run workflow (optionally `test_to`). It sends one sample
letter marked TEST to the project mailbox, logs in over IMAP and looks for it, then prints who would be asked
from the live database and the review list. No clerk is contacted. Locally: `gacrawl records test [--to ADDR]`.

Off until all of these are set: `openrecords.enabled: true` and `openrecords.requester` in
`config/settings.yaml`, and repository secrets `ORR_SMTP_HOST`, `ORR_SMTP_PORT`, `ORR_USER`, `ORR_PASSWORD`,
`ORR_FROM` (and `ORR_IMAP_HOST` to read replies). `gacrawl records plan --show` previews who would be asked and
the exact letter; `gacrawl records status` shows where every request stands.

## Coverage: which government bodies are crawled

- **Local:** all 159 counties, all 537 municipalities (cities, towns, the consolidated governments) and 180 school
  districts are in `config/jurisdictions.yaml`; the seed finds each one's official website (GMA directory,
  Wikidata, Wikipedia, Electo) and its meeting portals. 781 of 876 have a website; the other 90 are small towns
  (populations of 22 to about 3,400) with no website listed anywhere the seed looks. `data/seed_gaps.md`
  (written after every seed run) lists them, and the sites that refuse the crawler or cannot be reached, each
  with what is still known and a stanza to fill in `config/overrides.yaml` (`website:`, or `enabled: false`
  with a note when a town truly has nothing to crawl); the next seed run picks an override up at once.
- **Regional:** the 12 regional commissions (`config/sources.yaml`, domains from the Department of Community
  Affairs' list).
- **Refused from GitHub's runners:** 69 official sites (Atlanta, Cherokee, Houston, Newton and Walton counties,
  Smyrna, Dunwoody, Milton, Decatur, Gainesville and 59 others) answer the crawler's first request with a 403,
  most of them with a bot-protection challenge, and the same happens on their newer domains and council
  sub-sites. Nearly all run on CivicPlus or Granicus govAccess, whose firewalls refuse data-center address
  ranges (GitHub's runners are Azure machines) rather than this crawler in particular. The crawler never works
  around a refusal; the honest way past it is a runner on an ordinary connection, which keeps the same
  identification, robots.txt and spacing rules: `bash scripts/selfhosted.sh` on any Linux machine at home (a
  Raspberry Pi or an old laptop will do) checks what it needs, tests those sites from its own connection and prints
  the registration steps; then the repository variable `CRAWL_RUNNER = self-hosted` moves the crawl shards there
  (`MERGE_RUNNER` the research, finance and Code-changes job too), at no Actions minutes. Delete the variable to go
  back to GitHub's runners; set it only while the machine stays on, since a run whose runner is offline waits for
  it. To check from home first:
  `curl -sI -A "GACivicCrawler/0.3 (+https://github.com/ClockworkBlack/Crawl)" https://www.atlantaga.gov/robots.txt`
  (a 200 there means the block is on the address, not the crawler). The block list (`state/blocked.json`) expires
  entries after a week; `gacrawl status` lists them.
- **State:** the General Assembly through legis.ga.gov's API (members, committees, bills, votes, acts' Code
  changes); the Governor, Lieutenant Governor, Attorney General (press and opinions), Secretary of State, Public
  Service Commission, Agriculture, Insurance and Labor commissioners, the campaign finance commission, the
  Department of Audits and Accounts and the Judicial Council (`config/sources.yaml`). Not crawled: the State
  Election Board (robots.txt disallows crawlers), house.ga.gov and senate.ga.gov (they refuse the request), and
  sites that block the crawler (sos.ga.gov pages, eCode360).

## Is the data filling? (`gacrawl coverage`)

After every run the merge job counts, per Electo feature, what the database and the state files hold and compares
it with the last run: `data/coverage.md` (the report) and `state/coverage.json` (the counts and a short history),
both kept in the private repository. A feature at zero or one that shrank by more than a tenth is listed first
under **Needs attention**, with what feeds it (district sites for school board members, legis update for roll
calls, the police news search for incidents). A drop right after a `config/purge.yaml` entry is expected. The
**Crawl health** rows count sources read completely, waiting for the off-peak window, refused, and marked failed
only because they ran out of time (should stay at 0: a source's time share now starts before its probe, and time
outs are not backed off). Check this report before trusting a feature, instead of auditing the database by hand.

Lessons the report was built from (November 2026): a probe that ran under the previous source's spent time share
"failed" 185 sources in one run; a county site's probe read the county's Legistar a second time (every DeKalb agenda
item stored twice); district websites from Wikipedia included an archive copy, a newspaper and another district's
high school; police department names taken from page headings ("Police Department") matched every police story in
the country; GDELT refuses shared runner addresses for minutes at a time. Each is fixed at the source and has a test
in `tests/test_scheduler_time.py`.

## What needs a human

- **Municode** (most Georgia codes) blocks crawlers: `gacrawl tasks` lists confirmed links to capture by hand
  (`gacrawl ocr`) or request from the clerk.
- **Facebook / Instagram**: no open read API; add posts by hand with a screenshot (`gacrawl posts add`).
- **Georgia campaign finance**: no bulk API; export CSVs into `imports/finance/<id>.csv` (or a bulk CSV with a
  recipient column), and confirm the Commission's search URL in `config/finance.yaml` after `gacrawl finance probe`.
- **Organizations**: registries say what a PAC or nonprofit is; add websites and hand notes in
  `config/organizations.yaml` for the rest. Georgia corporate officers need an OpenCorporates key or a lookup by hand.
- **Leads** and **donor-matter patterns**: open both records and confirm it is the same entity before citing.
- **Transcript lines**: speaker labels are what the transcript says; items are marked needs_review.
- **Photos**: the crawler never collects photos of candidates or other public figures (Electo's rule too).

## Tests

```bash
pytest -q   # offline: local servers, synthetic Electo data, generated screenshots and PDFs
```

`tests/test_cli.py` builds the command line and parses every `gacrawl ...` line in the workflows, so a renamed flag
or a duplicated option fails here rather than on the runner at night.

## Credits

Thanks to **NinaBEz**. She knows why.


## Automatic import into Electo

Every nightly or full run ends with two pushes to Electo's `main` using the `ELECTO_TOKEN` secret: the packets
(`data/crawl/`; Electo's own `crawl-policing` import of them is switched off, since this job and the police job import
those files themselves), then **Import into Electo**, which clones Electo and runs its own importer against the complete
bundle (profiles with stances and controversies, councils, schools, places, city-run results, the records review), the
bill list (`civics/refresh_acts.py`), the Code as amended (`civics/apply_acts.py`) and the name registry
(`ga/names.py`), and commits what changed (`ga/crawl/`, `ga/councils.json`, `ga/places.json`,
`ga/pipeline_archive/{profiles,names,crawl_elections}.json`, `ga/pipeline_civics/{acts.json,ocga_cur/}`,
`research/crawl_*.json`; the News tab is left to the `news` job). It is `continue-on-error`: a failure there never fails
the crawl. The public site (GitHub Pages from `electo-data`) is then rebuilt from Electo's main by the same job, and by
`publish-site` for any other change to Electo; the page's Live results by the `results` job. Only the claude.ai copy of
the page needs a republish by hand. What runs by itself and what still needs a person: Electo's `HANDOFF.md`, *Nov 9
(evening): running on its own*.

## Public runner, private data

`config/runner.json` and `scripts/ci_runner.sh`: the code is copied (hourly, by the `sync-code` workflow running in the public repository,
main) to a public repository whose Actions minutes are free, and that repository runs the crawl and police
schedules. Everything gathered stays here: each job reads `state/` from this repository and writes it back here,
the database and the Electo bundle are this repository's release assets, records-request deadline issues open here,
and what passes between jobs is encrypted with the `DATA_TOKEN` secret (anyone can download a public repository's
run attachments). While `active` is true this repository's scheduled runs stand down; runs started by hand still
work. The public run logs show counts and page addresses, never record contents. Set `active` to false to bring
everything back here.
