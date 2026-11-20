# Crawl: standing instructions

## Formats: read before you change any data
Every format this repository writes or reads is written down in **`formats/`** (index: `formats/README.md`; for code:
`formats/registry.json`): the export bundle for Electo (`formats/bundle/`), the database's records by kind
(`formats/records/`), its tables and kv keys (`formats/store/`), state files (`formats/state/`), config
(`formats/config/`, the workflows included), committed data (`formats/data/`) and imported files (`formats/work/`).
The site's formats are in Electo's `formats/`, the logs' in LogView's `formats/`.

1. Before writing code that writes or reads a file, a record kind, a kv key or a config key, read its schema.
2. Change the schema in the same commit as the code. A new file, kind or key gets its schema in that commit.
3. Closed lists (kinds, statuses, levels, classes) come from the code's constants; a pinned schema (`x-pinned`) is
   compared with them by the tests.
4. Dates: a day is `YYYY-MM-DD`, a moment `YYYY-MM-DDTHH:MM:SSZ` (UTC).
5. Then: `python3 -I formats/index.py` and `python3 -m pytest -q tests/test_formats.py` (and the whole suite).
   `formats/check.py` and `formats/index.py` are the same files in Crawl, Electo and LogView: change all three.
6. Data that does not fit is fixed at its writer, or the schema is fixed with a line saying what was learned. A schema
   is never loosened without saying why.

The plan for strict formats feed by feed: `docs/DATA_FORMAT_PLAN.md`. Open questions for the owner:
`docs/OPEN_ITEMS.md` (notes; `docs/` is never copied to the public runner).

## How the crawler reads the web
- robots.txt is obeyed. No CAPTCHA or challenge is solved, no login, no paywall, and the crawler never disguises
  what it is (its User-Agent names it and this repository).
- A site that refuses the crawler is read only from copies the Internet Archive already holds; nothing is ever asked
  to be saved there (no Save Page Now).
- No photos of public figures are taken or kept.

## Where things run
- The code runs in the public repository ClockworkBlack/Crawl-runner (sync-code copies it there); its logs show counts
  only, never private data. records-send, records-test and mail-send run in this private repository only.
- Nothing private goes into a file sync-code copies: no mailbox address, no personal email address, no letter's
  signature, no secret (sync-code's own check refuses a copy that holds them).
- Every run's logs are kept in ClockworkBlack/LogView; a failure there names its class, cause and issue.
