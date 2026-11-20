# Formats: Crawl

Every schema and format the crawler: what it stores (the database's records, kv keys), keeps between runs (state/), is set by (config/), commits beside its state (data/) and hands Electo (the export bundle, dist/electo). One JSON Schema per format; this page and `registry.json` are written from them by `formats/index.py`. 230 formats (134 closed: every key listed; 1 pinned to the code's own lists).

The other registries: Electo: `ClockworkBlack/Electo formats/`; LogView: `ClockworkBlack/LogView formats/`.

## How a format is changed (every change, by anyone)

1. **Read the format first.** Before writing code that writes or reads a file, a record kind, a kv key or a config key,
   find it below and read its schema. If it is not listed, it is new: add its schema in the same change.
2. **Change the schema in the same commit as the code.** A key added, renamed, dropped or given another type or meaning
   is a change of the format: the schema says it, the writer writes it, and every reader listed reads it. A change
   that readers cannot take at once gets a new version (`format`/`v` in the file and `x-version` in the schema).
3. **Keys and values come from fixed lists.** Where the code holds the list (levels, kinds, statuses, reasons, classes),
   the schema's `enum` is that list and a test compares the two (`x-pinned`). A value that is not on the list is
   a fault, or a new value added to both in one change.
4. **Dates are ISO 8601**: a day `YYYY-MM-DD`, a moment `YYYY-MM-DDTHH:MM:SSZ` (UTC). A file says when it was made
   (`as_of`, a day, or `at`/`built`, a moment) under the name its schema gives. New files use `as_of` and `at` only.
5. **Rebuild this index** (`python3 -I formats/index.py`) and run the tests (`formats` tests check every schema
   loads, every schema here is listed, every file in this repository that has a schema fits it, and the pinned
   lists match the code).
6. **Real data that does not fit** is either a fault of the data (fixed at its writer, and left out at the reader's
   door with a counted reason: Electo's ledger `ga/crawl/left_out.json`) or a fault of the schema (fixed here, with a
   line in its `x-widened` or `description` saying what was learned). Never silence a fault by loosening a schema
   without saying why.

Schemas are JSON Schema (draft 2020-12), the subset `formats/check.py` reads; it needs only the standard library:

    python3 -I formats/check.py formats/<area>/<name>.schema.json path/to/data.json

Annotations: `x-writer`/`x-readers` (the code), `x-version`, `x-documented-in`, `x-observed` (what the schema was
measured on), `x-issues` (inconsistencies known when the registry was made, Nov 20, 2026), `x-widened` (what real
data showed that the first draft lacked), `x-values` (values seen, not a closed list), `x-pinned` (its lists are the
code's), `x-columns` (the column names of positional rows).


## Export bundle (Crawl -> Electo)

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `dist/electo/ballot_questions.json` | [ballot_questions.schema.json](bundle/ballot_questions.schema.json) | 2.3 | open | ballot.export_questions |
| `dist/electo/bill_reasons.json` | [bill_reasons.schema.json](bundle/bill_reasons.schema.json) | 2.3 | closed, 1 known issue(s) | bill_reasons.export_reasons |
| `dist/electo/blocked.json` | [blocked.schema.json](bundle/blocked.schema.json) |  | closed | export.export() copy of BlockList(state/blocked.json).data |
| `dist/electo/candidates.json` | [candidates.schema.json](bundle/candidates.schema.json) | 2.3 | open | export.export() |
| `dist/electo/candidates/<id>.json` | [candidates__id.schema.json](bundle/candidates__id.schema.json) | 2.3 | open | export.candidate_file |
| `dist/electo/code_acts.json` | [code_acts.schema.json](bundle/code_acts.schema.json) | parser 4 | open | codeacts.export_code_acts |
| `dist/electo/connections.json` | [connections.schema.json](bundle/connections.schema.json) | 2.3 | closed, 1 known issue(s) | connections.export_graph |
| `dist/electo/councils/<id>.json` | [councils__id.schema.json](bundle/councils__id.schema.json) | 2.3 | closed, 1 known issue(s) | export.council_alignment |
| `dist/electo/donors.json` | [donors.schema.json](bundle/donors.schema.json) | 2.3 | open, 1 known issue(s) | donors.export_donors |
| `dist/electo/elections.json` | [elections.schema.json](bundle/elections.schema.json) | 2.3 (Electo archive  | closed | elections.electo_elections / electo_qualifying |
| `dist/electo/federal_bills/<congress>.json` | [federal_bills__congress.schema.json](bundle/federal_bills__congress.schema.json) |  | open | federal_bills.export_federal |
| `dist/electo/federal_bills/meta.json` | [federal_bills__meta.schema.json](bundle/federal_bills__meta.schema.json) |  | open | federal_bills.export_federal |
| `dist/electo/jurisdictions.json` | [jurisdictions.schema.json](bundle/jurisdictions.schema.json) | 2.3 | closed, 2 known issue(s) | export.export() (registry.jurisdictions + progress.json status + record counts) |
| `dist/electo/jurisdictions/<id>.json` | [jurisdictions__id.schema.json](bundle/jurisdictions__id.schema.json) | 2.3 | open, 2 known issue(s) | export._write_jurisdiction (streamed JSON) |
| `dist/electo/legislation.json` | [legislation.schema.json](bundle/legislation.schema.json) | 2.3 | open | codeacts.export_legislation |
| `dist/electo/manifest.json` | [manifest.schema.json](bundle/manifest.schema.json) | schema_version 2.3 ( | closed, 1 known issue(s) | gacrawl/export.py export() |
| `dist/electo/media.json` | [media.schema.json](bundle/media.schema.json) | 2.3 | closed, 1 known issue(s) | police.export_policing |
| `dist/electo/meeting_summaries.json` | [meeting_summaries.schema.json](bundle/meeting_summaries.schema.json) |  | open, 2 known issue(s) | meeting_summaries.export_summaries |
| `dist/electo/news.json` | [news.schema.json](bundle/news.schema.json) |  | open, 1 known issue(s) | newsdigest.export_news |
| `dist/electo/open_records.json` | [open_records.schema.json](bundle/open_records.schema.json) |  | open, 1 known issue(s) | export.export_open_records (openrecords.Tracker data/openrecords/requests.json) |
| `dist/electo/organizations.json` | [organizations.schema.json](bundle/organizations.schema.json) | 2.3 | open | export.export() (slim organization records) |
| `dist/electo/packets/index.json` | [packets__index.schema.json](bundle/packets__index.schema.json) | 2.3 | closed | electo.write_packet_index |
| `dist/electo/packets/<key>.json` | [packets__key.schema.json](bundle/packets__key.schema.json) | 2.3 | open, 2 known issue(s) | electo.packet_for |
| `dist/electo/police.json` | [police.schema.json](bundle/police.schema.json) | 2.3 | closed, 1 known issue(s) | police.export_policing |
| `dist/electo/police_incidents.json` | [police_incidents.schema.json](bundle/police_incidents.schema.json) | 2.3 | closed, 1 known issue(s) | policing_news.export_incidents |
| `dist/electo/profiles/index.json` | [profiles__index.schema.json](bundle/profiles__index.schema.json) | 2.3 | closed, 1 known issue(s) | profiles.ProfileWriter.finish |
| `dist/electo/profiles/<key>.json` | [profiles__key.schema.json](bundle/profiles__key.schema.json) | 2.3 | closed, 2 known issue(s) | profiles.ProfileWriter.add (ProfileBuilder.profile) |
| `dist/electo/records_contacts.json` | [records_contacts.schema.json](bundle/records_contacts.schema.json) | 2.3 | closed, 1 known issue(s) | police.export_policing |
| `dist/electo/rumor_mill.json` | [rumor_mill.schema.json](bundle/rumor_mill.schema.json) | 2.3 | closed | export.export() from candidates.RumorMill (data/rumor_mill/rumors.jsonl), meta.text dropped |
| `dist/electo/rumor_mill_video.json` | [rumor_mill_video.schema.json](bundle/rumor_mill_video.schema.json) | 2.3 | closed | export.export() from rumor_video.VideoRumorMill (data/rumor_mill/videos.jsonl) |
| `dist/electo/schema.md` | [schema.schema.json](bundle/schema.schema.json) | 2.3 | closed | gacrawl/export.py (SCHEMA_MD.format(version) + agencies.STATE_MEETINGS_SCHEMA) |
| `dist/electo/school_bounds.json` | [school_bounds.schema.json](bundle/school_bounds.schema.json) |  | closed | schools.export_schools (copy of state/school_bounds.json) |
| `dist/electo/schools.json` | [schools.schema.json](bundle/schools.schema.json) | 2.3 | open | schools.export_schools |
| `dist/electo/schools/<district>.json` | [schools__district.schema.json](bundle/schools__district.schema.json) | 2.3 | open | schools.export_schools |
| `dist/electo/screenshots.json` | [screenshots.schema.json](bundle/screenshots.schema.json) | 2.3 | closed | screens.Inbox.box_export |
| `dist/electo/state_meetings.json` | [state_meetings.schema.json](bundle/state_meetings.schema.json) | format 2 (agencies.F | open, pinned, 3 known issue(s) | agencies.export_state_meetings (validate_body / validate_meeting) |
| `dist/electo/verified_posts.json` | [verified_posts.schema.json](bundle/verified_posts.schema.json) |  | closed | verified_posts.Registry.posts (data/verified/posts.jsonl) |
| `dist/electo/vote_model.json` | [vote_model.schema.json](bundle/vote_model.schema.json) | 2.3 | closed, 1 known issue(s) | vote_model.export_vote_model |

## Config

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `config/agencies.yaml` | [agencies.schema.json](config/agencies.schema.json) |  | closed, 1 known issue(s) | hand |
| `config/backfill.json` | [backfill.schema.json](config/backfill.schema.json) |  | closed | hand |
| `config/candidates.yaml` | [candidates.schema.json](config/candidates.schema.json) |  | closed | generated by Electo tools/crawl_candidates.py (+ hand entries) |
| `config/channel_decisions.yaml` | [channel_decisions.schema.json](config/channel_decisions.schema.json) |  | closed | hand (owner) |
| `config/committee_links.yaml` | [committee_links.schema.json](config/committee_links.schema.json) |  | closed | hand |
| `config/district_calendars.yaml` | [district_calendars.schema.json](config/district_calendars.schema.json) |  | closed | hand/generated |
| `config/finance.yaml` | [finance.schema.json](config/finance.schema.json) |  | closed | hand |
| `.github/workflows/*.yml` | [github-workflows.schema.json](config/github-workflows.schema.json) |  | open | by hand |
| `config/guides.yaml` | [guides.schema.json](config/guides.schema.json) |  | closed | hand |
| `config/issues.yaml` | [issues.schema.json](config/issues.schema.json) |  | closed | hand |
| `config/jurisdictions.yaml` | [jurisdictions.schema.json](config/jurisdictions.schema.json) |  | closed | generated (scripts/build_registry.py) |
| `config/legislature.yaml` | [legislature.schema.json](config/legislature.schema.json) |  | closed | hand |
| `config/organizations.yaml` | [organizations.schema.json](config/organizations.schema.json) |  | closed | hand |
| `config/outlets.yaml` | [outlets.schema.json](config/outlets.schema.json) |  | closed | hand |
| `config/overrides.yaml` | [overrides.schema.json](config/overrides.schema.json) |  | closed | hand |
| `config/press.yaml` | [press.schema.json](config/press.schema.json) |  | closed | hand |
| `config/private/openrecords.yaml` | [private__openrecords.schema.json](config/private__openrecords.schema.json) |  | closed | hand (private) |
| `config/private/settings.yaml` | [private__settings.schema.json](config/private__settings.schema.json) |  | closed | hand (private) |
| `config/purge.yaml` | [purge.schema.json](config/purge.schema.json) |  | closed | hand |
| `config/runner.json` | [runner.schema.json](config/runner.schema.json) |  | closed | hand |
| `config/school_districts.yaml` | [school_districts.schema.json](config/school_districts.schema.json) |  | closed | generated (scripts/build_school_districts.py) |
| `config/schools.yaml` | [schools.schema.json](config/schools.schema.json) |  | closed | hand |
| `config/settings.yaml` | [settings.schema.json](config/settings.schema.json) |  | closed, 1 known issue(s) | hand |
| `config/sources.yaml` | [sources.schema.json](config/sources.schema.json) |  | closed | hand |
| `config/topics.yaml` | [topics.schema.json](config/topics.schema.json) |  | closed | hand |

## Data files committed with the state

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `data/openrecords/found.json` | [openrecords__found.schema.json](data/openrecords__found.schema.json) |  | closed | openrecords / ordinance_files |
| `data/openrecords/requests.json` | [openrecords__requests.schema.json](data/openrecords__requests.schema.json) |  | closed | openrecords.Tracker |
| `data/rumor_mill/rumors.jsonl` | [rumor_mill__rumors.schema.json](data/rumor_mill__rumors.schema.json) |  | closed | candidates.RumorMill, rumors_ai |
| `data/rumor_mill/videos.jsonl` | [rumor_mill__videos.schema.json](data/rumor_mill__videos.schema.json) |  | closed | rumor_video.VideoRumorMill |
| `data/train/channel_log.jsonl` | [train__channel_log.schema.json](data/train__channel_log.schema.json) |  | closed | channel_ai (llm.log) |
| `data/train/discover_log.jsonl` | [train__discover_log.schema.json](data/train__discover_log.schema.json) |  | closed, 1 known issue(s) | discover_ai |
| `data/train/donor_about_log.jsonl` | [train__donor_about_log.schema.json](data/train__donor_about_log.schema.json) |  | closed | donors_ai |
| `data/train/federal_bill_log.jsonl` | [train__federal_bill_log.schema.json](data/train__federal_bill_log.schema.json) |  | closed | federal_bills AI |
| `data/train/minutes_log.jsonl` | [train__minutes_log.schema.json](data/train__minutes_log.schema.json) |  | closed, 1 known issue(s) | meeting_summaries |
| `data/train/rumor_log.jsonl` | [train__rumor_log.schema.json](data/train__rumor_log.schema.json) |  | closed, 1 known issue(s) | rumors_ai |
| `data/verified/accounts.jsonl` | [verified__accounts.schema.json](data/verified__accounts.schema.json) |  | closed | verified_posts.Registry |
| `data/verified/posts.jsonl` | [verified__posts.schema.json](data/verified__posts.schema.json) |  | closed | verified_posts.Registry |

## Database records, by kind (meta keys)

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `agenda_item` | [agenda_item.schema.json](records/agenda_item.schema.json) |  | open | minutes.py:337 _derive; sources/boarddocs.py:201 records; sources/legistar.py:176 _item_records |
| `appointment` | [appointment.schema.json](records/appointment.schema.json) |  | open, 1 known issue(s) | press.py:103 _derive |
| `article` | [article.schema.json](records/article.schema.json) |  | open | candidates.py:1060 articles_batch; candidates.py:1094 _leads; discover_ai.py:590 keep; webresearch.py:208 outl |
| `ballot_question` | [ballot_question.schema.json](records/ballot_question.schema.json) |  | open, 1 known issue(s) | ballot.py:72 _derive; elections.py:484 _derive |
| `bill` | [bill.schema.json](records/bill.schema.json) |  | open, 1 known issue(s) | sources/legis.py:196 bill_record |
| `bill_reason` | [bill_reason.schema.json](records/bill_reason.schema.json) |  | open, 1 known issue(s) | bill_reasons.py:270 bill; bill_reasons.py:364 vetoes; legis_video.py:449 hearings |
| `bill_video` | [bill_video.schema.json](records/bill_video.schema.json) |  | open, 1 known issue(s) | legis_video.py:333 link_videos |
| `budget_doc` | [budget_doc.schema.json](records/budget_doc.schema.json) |  | open, 1 known issue(s) | qbe.py:285 run; schools.py:1663 budgets |
| `code_act` | [code_act.schema.json](records/code_act.schema.json) |  | open, 1 known issue(s) | codeacts.py:785 run |
| `connection` | [connection.schema.json](records/connection.schema.json) |  | open | candidates.py:889 _statement; guides.py:160 _derive; guides.py:174 _derive |
| `contribution` | [contribution.schema.json](records/contribution.schema.json) |  | open, 1 known issue(s) | finance.py:345 contribution_record |
| `document` | [document.schema.json](records/document.schema.json) |  | open | simbli_archive.py:269 district; sources/civicclerk.py:264 _open_file; sources/civicclerk.py:271 _open_file |
| `election` | [election.schema.json](records/election.schema.json) |  | open, 1 known issue(s) | written by channels.py page crawl for registry source <jid>:elections (src kind 'election') |
| `election_result` | [election_result.schema.json](records/election_result.schema.json) |  | open, 1 known issue(s) | elections.py:491 _derive |
| `endorsement` | [endorsement.schema.json](records/endorsement.schema.json) |  | open, 1 known issue(s) | guides.py:155 _derive |
| `federal_bill` | [federal_bill.schema.json](records/federal_bill.schema.json) |  | open, 1 known issue(s) | federal_bills.py:260 _save |
| `feed_item` | [feed_item.schema.json](records/feed_item.schema.json) |  | open | channels.py:1009 crawl |
| `guide` | [guide.schema.json](records/guide.schema.json) |  | open, 1 known issue(s) | channels.py for config/guides.yaml sources |
| `guide_answer` | [guide_answer.schema.json](records/guide_answer.schema.json) |  | open, 1 known issue(s) | guides.py:185 _derive |
| `(kind from caller / source spec)` | [kind-from-caller-source-spec.schema.json](records/kind-from-caller-source-spec.schema.json) |  | open, 1 known issue(s) | archive_sources.py:149 source; channels.py:734 _doc_record; channels.py:750 _doc_record; channels.py:752 _doc_ |
| `lead` | [lead.schema.json](records/lead.schema.json) |  | open | candidates.py:1053 articles_batch; candidates.py:1088 _leads |
| `legislator` | [legislator.schema.json](records/legislator.schema.json) |  | open, 1 known issue(s) | sources/legis.py:162 legislator_record |
| `matter` | [matter.schema.json](records/matter.schema.json) |  | open | sources/legistar.py:107 _matter |
| `meeting` | [meeting.schema.json](records/meeting.schema.json) |  | open | agencies.py:1853 run; agencies.py:1879 run; board_schedules.py:136 _derive; district_calendars.py:1258 store_e |
| `meeting_summary` | [meeting_summary.schema.json](records/meeting_summary.schema.json) |  | open, 1 known issue(s) | meeting_summaries.py:136 minutes_run |
| `mention` | [mention.schema.json](records/mention.schema.json) |  | open | candidates.py:754 _mentions |
| `news_item` | [news_item.schema.json](records/news_item.schema.json) |  | open, 1 known issue(s) | newsdigest.py:372 add |
| `official` | [official.schema.json](records/official.schema.json) |  | open | sources/legistar.py:487 officials |
| `ordinance` | [ordinance.schema.json](records/ordinance.schema.json) |  | open | sources/legistar.py:107 _matter |
| `organization` | [organization.schema.json](records/organization.schema.json) |  | open, 1 known issue(s) | finance.py:1144 _enrich_one; finance.py:1199 enrich |
| `page` | [page.schema.json](records/page.schema.json) |  | open | schools.py:1031 sites; schools.py:1124 district_people_pages |
| `police_incident` | [police_incident.schema.json](records/police_incident.schema.json) |  | open, 1 known issue(s) | policing_news.py:517 department; policing_news.py:540 _from_outlet_apis |
| `policy` | [policy.schema.json](records/policy.schema.json) |  | open, 1 known issue(s) | simbli_archive.py:244 district |
| `post` | [post.schema.json](records/post.schema.json) |  | open | candidates.py:821 _posts; channels.py:966 crawl |
| `press_release` | [press_release.schema.json](records/press_release.schema.json) |  | open, 1 known issue(s) | channels.py for config/press.yaml sources |
| `qualifying` | [qualifying.schema.json](records/qualifying.schema.json) |  | open, 1 known issue(s) | elections.py:501 _derive |
| `rating` | [rating.schema.json](records/rating.schema.json) |  | open, 1 known issue(s) | guides.py:169 _derive |
| `school` | [school.schema.json](records/school.schema.json) |  | open, 1 known issue(s) | schools.py:241 school_record |
| `school_stat` | [school_stat.schema.json](records/school_stat.schema.json) |  | open, 1 known issue(s) | schools.py:668 stat_record |
| `screenshot` | [screenshot.schema.json](records/screenshot.schema.json) |  | open, 1 known issue(s) | screens.py:354 ingest |
| `social_mention` | [social_mention.schema.json](records/social_mention.schema.json) |  | open | candidates.py:1116 _social_mentions |
| `sponsorship` | [sponsorship.schema.json](records/sponsorship.schema.json) |  | open, 1 known issue(s) | candidates.py:799 _sponsorships |
| `statement` | [statement.schema.json](records/statement.schema.json) |  | open | candidates.py:960 _archived_site |
| `transcript` | [transcript.schema.json](records/transcript.schema.json) |  | open, 1 known issue(s) | transcripts.py:121 cues_to_record; video_ai.py:242 run |
| `video` | [video.schema.json](records/video.schema.json) |  | open, 1 known issue(s) | transcripts.py:397 youtube_records |
| `vote` | [vote.schema.json](records/vote.schema.json) |  | open | candidates.py:772 _votes; minutes.py:341 _derive; sources/legis.py:230 roll_calls; sources/legiscan.py:196 rec |

## State files (kept between runs)

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `state/ai_seeds.json` | [ai_seeds.schema.json](state/ai_seeds.schema.json) |  | closed | discover_ai |
| `state/backfill.json` | [backfill.schema.json](state/backfill.schema.json) |  | closed | backfill.py |
| `state/blocked.json` | [blocked.schema.json](state/blocked.schema.json) |  | closed, 1 known issue(s) | blocked.BlockList (fetch.py records refusals) |
| `state/cadence.json` | [cadence.schema.json](state/cadence.schema.json) |  | closed | scripts/cadence.py done |
| `state/candidate_progress.json` | [candidate_progress.schema.json](state/candidate_progress.schema.json) |  | closed | candidates / webresearch (scheduler.Progress) |
| `state/channel_ai.json` | [channel_ai.schema.json](state/channel_ai.schema.json) |  | closed | channel_ai |
| `state/channels.json` | [channels.schema.json](state/channels.schema.json) |  | closed | channels.py (channel probe) |
| `state/coverage.json` | [coverage.schema.json](state/coverage.schema.json) |  | closed | coverage.py |
| `state/discovered.json` | [discovered.schema.json](state/discovered.schema.json) |  | closed | seed.py (registry discovery) |
| `state/district_calendars.json` | [district_calendars.schema.json](state/district_calendars.schema.json) |  | closed, 1 known issue(s) | district_calendars.py |
| `state/electo_codes.json` | [electo_codes.schema.json](state/electo_codes.schema.json) |  | closed | openrecords/cli |
| `state/electo_people.json` | [electo_people.schema.json](state/electo_people.schema.json) |  | closed | scripts/electo_people.sh / cli |
| `state/entities.json` | [entities.schema.json](state/entities.schema.json) |  | closed | legacy (now kv 'entities') |
| `state/finance_portal.json` | [finance_portal.schema.json](state/finance_portal.schema.json) |  | closed, 1 known issue(s) | finance.py probe (listed in crawl.yml Commit state) |
| `state/history.json` | [history.schema.json](state/history.schema.json) |  | closed | history/run.py |
| `state/http_cache/` | [http_cache__.schema.json](state/http_cache__.schema.json) |  | closed | fetch.py HTTP cache |
| `state/legis_people.json` | [legis_people.schema.json](state/legis_people.schema.json) |  | closed | cli (rebuilt nightly; read by export packets) |
| `state/legis_refused.json` | [legis_refused.schema.json](state/legis_refused.schema.json) |  | closed | sources/legis.py |
| `state/media.json` | [media.schema.json](state/media.schema.json) |  | closed | police.py |
| `state/news_feeds.json` | [news_feeds.schema.json](state/news_feeds.schema.json) |  | closed | newsdigest.feed_of |
| `state/ordinance_codes.json` | [ordinance_codes.schema.json](state/ordinance_codes.schema.json) |  | closed | openrecords (gacrawl records codes) |
| `state/orgs.json` | [orgs.schema.json](state/orgs.schema.json) |  | closed | legacy (now kv 'orgs') |
| `state/police.json` | [police.schema.json](state/police.schema.json) |  | closed | police.py |
| `state/police_news.json` | [police_news.schema.json](state/police_news.schema.json) |  | closed, 1 known issue(s) | policing_news |
| `state/police_roster.json` | [police_roster.schema.json](state/police_roster.schema.json) |  | closed | police.py |
| `state/profile_inputs.json` | [profile_inputs.schema.json](state/profile_inputs.schema.json) |  | closed | legacy (now kv 'profile_inputs') |
| `state/progress.json` | [progress.schema.json](state/progress.schema.json) |  | closed, 1 known issue(s) | scheduler.Progress |
| `state/research_last.json` | [research_last.schema.json](state/research_last.schema.json) |  | closed | candidates/webresearch |
| `state/school_bounds.json` | [school_bounds.schema.json](state/school_bounds.schema.json) |  | closed | schools seed |
| `state/school_people.json` | [school_people.schema.json](state/school_people.schema.json) |  | closed | schools people |
| `state/schools.json` | [schools.schema.json](state/schools.schema.json) |  | closed | schools seed |
| `state/screens_seen.json` | [screens_seen.schema.json](state/screens_seen.schema.json) |  | closed | screens.Inbox |
| `state/sheriffs.json` | [sheriffs.schema.json](state/sheriffs.schema.json) |  | closed | sheriffs.py |
| `state/veto_statements.json` | [veto_statements.schema.json](state/veto_statements.schema.json) |  | closed | bill_reasons.vetoes |
| `state/websearch.json` | [websearch.schema.json](state/websearch.schema.json) |  | closed | websearch.py |
| `state/wp_outlets.json` | [wp_outlets.schema.json](state/wp_outlets.schema.json) |  | closed | newsdigest / webresearch (WP API probe) |

## Database tables and kv keys

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `data/crawl.db: cursors table` | [crawl.db-cursors-table.schema.json](store/crawl.db-cursors-table.schema.json) | 2 | open | store.set_cursor |
| `data/crawl.db: kv table` | [crawl.db-kv-table.schema.json](store/crawl.db-kv-table.schema.json) | 2 | open | store.kv_set (see area kv) |
| `data/crawl.db: records table` | [crawl.db-records-table.schema.json](store/crawl.db-records-table.schema.json) | store.SCHEMA_VERSION | open, 1 known issue(s) | store.Store.add / add_many (and shard.merge_delta) |
| `data/crawl.db: urls table` | [crawl.db-urls-table.schema.json](store/crawl.db-urls-table.schema.json) | 2 | open | store.mark_url |
| `gacrawl.store.Record (dataclass)` | [gacrawl.store.Record-dataclass.schema.json](store/gacrawl.store.Record-dataclass.schema.json) |  | open, 2 known issue(s) | every reader (91 Record( calls) |
| `agency_archive:{agency_id}:{page_url}` | [kv__agency_archive-agency_id-page_url.schema.json](store/kv__agency_archive-agency_id-page_url.schema.json) |  | open, 1 known issue(s) | agencies.read_body / run |
| `agency_read:{agency_id}` | [kv__agency_read-agency_id.schema.json](store/kv__agency_read-agency_id.schema.json) |  | closed | agencies.run |
| `agency_via:{agency_id}` | [kv__agency_via-agency_id.schema.json](store/kv__agency_via-agency_id.schema.json) |  | closed | agencies.run |
| `archive_src:{source_id}` | [kv__archive_src-source_id.schema.json](store/kv__archive_src-source_id.schema.json) |  | open | archive_sources |
| `archived_site:{source_id}` | [kv__archived_site-source_id.schema.json](store/kv__archived_site-source_id.schema.json) |  | open | candidates._archived_site |
| `bill_history:{legislation_id}` | [kv__bill_history-legislation_id.schema.json](store/kv__bill_history-legislation_id.schema.json) |  | closed | legis_video |
| `bill_history_at:{legislation_id}` | [kv__bill_history_at-legislation_id.schema.json](store/kv__bill_history_at-legislation_id.schema.json) |  | closed | legis_video |
| `bill_reasons:{legislation_id}` | [kv__bill_reasons-legislation_id.schema.json](store/kv__bill_reasons-legislation_id.schema.json) |  | closed | bill_reasons |
| `codeacts:{legislation_id}` | [kv__codeacts-legislation_id.schema.json](store/kv__codeacts-legislation_id.schema.json) |  | open | codeacts.run |
| `dead_site:{source_id}` | [kv__dead_site-source_id.schema.json](store/kv__dead_site-source_id.schema.json) |  | closed | candidates |
| `derive:{name}:v` | [kv__derive-name-v.schema.json](store/kv__derive-name-v.schema.json) |  | closed | derive |
| `derive:{name}` | [kv__derive-name.schema.json](store/kv__derive-name.schema.json) |  | closed | derive |
| `donor_about` | [kv__donor_about.schema.json](store/kv__donor_about.schema.json) |  | closed | donors_ai; model_handoff (KV_KEYS) |
| `elections:report:{jurisdiction}:{iso}` | [kv__elections-report-jurisdiction-iso.schema.json](store/kv__elections-report-jurisdiction-iso.schema.json) |  | open | election_reports |
| `entities` | [kv__entities.schema.json](store/kv__entities.schema.json) |  | open | entities.Entities.flush |
| `fec:a2:{committee_id}` | [kv__fec-a2-committee_id.schema.json](store/kv__fec-a2-committee_id.schema.json) |  | closed | finance; cli apply (newer wins) |
| `fec:a2r:{committee_id}` | [kv__fec-a2r-committee_id.schema.json](store/kv__fec-a2r-committee_id.schema.json) |  | open | finance; cli apply |
| `fec:drop:{committee_id}` | [kv__fec-drop-committee_id.schema.json](store/kv__fec-drop-committee_id.schema.json) |  | closed | finance |
| `fec_ids2` | [kv__fec_ids2.schema.json](store/kv__fec_ids2.schema.json) |  | open | finance (FEC id lookup); fec-reread apply (cli) |
| `fec_reread_applied` | [kv__fec_reread_applied.schema.json](store/kv__fec_reread_applied.schema.json) |  | closed | cli (fec-reread apply) |
| `fec_totals` | [kv__fec_totals.schema.json](store/kv__fec_totals.schema.json) |  | open | finance._fec_totals; cli apply |
| `fedbills:{congress}:{type}` | [kv__fedbills-congress-type.schema.json](store/kv__fedbills-congress-type.schema.json) |  | open | federal_bills |
| `federal_bill_ai` | [kv__federal_bill_ai.schema.json](store/kv__federal_bill_ai.schema.json) |  | open | federal_bills (AI pass); model_handoff (KV_KEYS) |
| `ga_ethics:{system}:{year|all}` | [kv__ga_ethics-system-year-all.schema.json](store/kv__ga_ethics-system-year-all.schema.json) |  | open | ethics (ga-money job) |
| `ga_money_applied` | [kv__ga_money_applied.schema.json](store/kv__ga_money_applied.schema.json) |  | closed | ethics |
| `ga_registry` | [kv__ga_registry.schema.json](store/kv__ga_registry.schema.json) |  | open | registries |
| `hearing_tx:{video_id}` | [kv__hearing_tx-video_id.schema.json](store/kv__hearing_tx-video_id.schema.json) |  | closed | legis_video.hearings |
| `irs_bmf` | [kv__irs_bmf.schema.json](store/kv__irs_bmf.schema.json) |  | closed | registries (IRS BMF cache); model_handoff |
| `legis:legislation:{session}` | [kv__legis-legislation-session.schema.json](store/kv__legis-legislation-session.schema.json) |  | closed | sources/legis.py, codeacts |
| `legis:meetings:read` | [kv__legis-meetings-read.schema.json](store/kv__legis-meetings-read.schema.json) |  | closed | legis_meetings.run |
| `legis:members:{session}` | [kv__legis-members-session.schema.json](store/kv__legis-members-session.schema.json) |  | closed | sources/legis.py |
| `legis:sessions` | [kv__legis-sessions.schema.json](store/kv__legis-sessions.schema.json) |  | open | legis_sessions |
| `legis:votes:{legislation_id}` | [kv__legis-votes-legislation_id.schema.json](store/kv__legis-votes-legislation_id.schema.json) |  | closed | sources/legis.py |
| `legis:votes_try:{legislation_id}` | [kv__legis-votes_try-legislation_id.schema.json](store/kv__legis-votes_try-legislation_id.schema.json) |  | closed | sources/legis.py |
| `legis_refusals` | [kv__legis_refusals.schema.json](store/kv__legis_refusals.schema.json) |  | open | sources/legis.py |
| `legiscan:dataset:{session_id}` | [kv__legiscan-dataset-session_id.schema.json](store/kv__legiscan-dataset-session_id.schema.json) |  | closed | sources/legiscan.py |
| `legiscan:read:{session_id}` | [kv__legiscan-read-session_id.schema.json](store/kv__legiscan-read-session_id.schema.json) |  | closed | sources/legiscan.py |
| `orgs` | [kv__orgs.schema.json](store/kv__orgs.schema.json) |  | open, 1 known issue(s) | finance.OrgEnricher.flush; model_handoff (KV_KEYS) |
| `profile_inputs` | [kv__profile_inputs.schema.json](store/kv__profile_inputs.schema.json) |  | closed | profiles.ProfileBuilder |
| `published_iso` | [kv__published_iso.schema.json](store/kv__published_iso.schema.json) |  | closed | store.normalize_published |
| `repair:feed_page_keys` | [kv__repair-feed_page_keys.schema.json](store/kv__repair-feed_page_keys.schema.json) |  | closed | cli |
| `repair:text_dates` | [kv__repair-text_dates.schema.json](store/kv__repair-text_dates.schema.json) |  | closed | cli |
| `repair:unrecorded_documents` | [kv__repair-unrecorded_documents.schema.json](store/kv__repair-unrecorded_documents.schema.json) |  | closed | cli |
| `schema` | [kv__schema.schema.json](store/kv__schema.schema.json) |  | closed | store.Store.__init__ |
| `schools:budget:{url}` | [kv__schools-budget-url.schema.json](store/kv__schools-budget-url.schema.json) |  | closed | schools |
| `schools:ccrpi:{url}` | [kv__schools-ccrpi-url.schema.json](store/kv__schools-ccrpi-url.schema.json) |  | closed | schools |
| `schools:gosa:{sid}` | [kv__schools-gosa-sid.schema.json](store/kv__schools-gosa-sid.schema.json) |  | closed | schools |
| `schools:people_pages:checked` | [kv__schools-people_pages-checked.schema.json](store/kv__schools-people_pages-checked.schema.json) |  | closed | schools |
| `schools:qbe:{rid}:{folder}:{org|state}` | [kv__schools-qbe-rid-folder-org-state.schema.json](store/kv__schools-qbe-rid-folder-org-state.schema.json) |  | closed | qbe |
| `schools:sites:checked` | [kv__schools-sites-checked.schema.json](store/kv__schools-sites-checked.schema.json) |  | closed | schools |
| `schools:sites` | [kv__schools-sites.schema.json](store/kv__schools-sites.schema.json) |  | closed | schools |
| `simbli:archive:{source}:pages` | [kv__simbli-archive-source-pages.schema.json](store/kv__simbli-archive-source-pages.schema.json) |  | closed | simbli_archive |
| `simbli:archive:{source}` | [kv__simbli-archive-source.schema.json](store/kv__simbli-archive-source.schema.json) |  | open | simbli_archive |
| `video_ai:blocked` | [kv__video_ai-blocked.schema.json](store/kv__video_ai-blocked.schema.json) |  | closed | video_ai |
| `video_ai:tried` | [kv__video_ai-tried.schema.json](store/kv__video_ai-tried.schema.json) |  | closed | video_ai |
| `video_ai:why` | [kv__video_ai-why.schema.json](store/kv__video_ai-why.schema.json) |  | closed | video_ai |
| `youtube:keyed` | [kv__youtube-keyed.schema.json](store/kv__youtube-keyed.schema.json) |  | closed | cli |
| `store.CONTENT_KEYED` | [store.CONTENT_KEYED.schema.json](store/store.CONTENT_KEYED.schema.json) |  | open | store.py constant |
| `store.ID_FIELDS` | [store.ID_FIELDS.schema.json](store/store.ID_FIELDS.schema.json) |  | open | store.py constant |
| `store.SOFT_META` | [store.SOFT_META.schema.json](store/store.SOFT_META.schema.json) |  | open | store.py constant |

## work

| Format | Schema | Version | Keys | Written by |
| --- | --- | --- | --- | --- |
| `data/ethics_explore/index.json` | [data__ethics_explore__index.schema.json](work/data__ethics_explore__index.schema.json) |  | open |  |
| `data/ethics_probe/bulk.json` | [data__ethics_probe__bulk.schema.json](work/data__ethics_probe__bulk.schema.json) |  | open |  |
| `data/openrecords/clerks.json` | [data__openrecords__clerks.schema.json](work/data__openrecords__clerks.schema.json) |  | open |  |
| `imports/ordinances/GA/index.json` | [imports__ordinances__GA__index.schema.json](work/imports__ordinances__GA__index.schema.json) |  | open |  |
| `imports/ordinances/GA/*/*/*.json` | [imports__ordinances__GA__place__code__file.schema.json](work/imports__ordinances__GA__place__code__file.schema.json) |  | open | the ordinance import (Ordinance Drop; tools beside imports/ordinances) |
| `imports/ordinances/GA/*/*/manifest.json` | [imports__ordinances__GA__place__code__manifest.schema.json](work/imports__ordinances__GA__place__code__manifest.schema.json) |  | closed | the ordinance import (Ordinance Drop; tools beside imports/ordinances) |
| `imports/ordinances/GA/*/*/nodes/*.json` | [imports__ordinances__GA__place__code__nodes.schema.json](work/imports__ordinances__GA__place__code__nodes.schema.json) |  | open | the ordinance import (Ordinance Drop; tools beside imports/ordinances) |
| `tests/fixtures/history/sos_*_*_two_columns.json` | [tests__fixtures__history__sos_N_N_two_columns.schema.json](work/tests__fixtures__history__sos_N_N_two_columns.schema.json) |  | open |  |
