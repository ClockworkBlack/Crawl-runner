#!/usr/bin/env bash
# Move the crawl database between a CI runner and the "crawl-data" release.
#   scripts/ci_db.sh pull                 download data/crawl.db
#   scripts/ci_db.sh push                 upload data/crawl.db, creating the release if needed
#   scripts/ci_db.sh fetch OUT [NAME]     the stored compressed file (default crawl.db.zst) to OUT, whole and checked
#   scripts/ci_db.sh store FILE NAME      a compressed file under another name (a temp save, a backup)
#   scripts/ci_db.sh drop NAME            remove a stored copy
# scripts/ci_db.py does the work and says how the file is stored (in parts under GitHub's 2 GiB limit, behind a list
# written last) and what stops a run: a database that could not be read is never taken for "no database yet", and a
# database that lost more than half is not stored over the one on the release.
# zstd (multi-threaded) compresses it. OPTIMIZE_FTS=true (the weekly run) rebuilds the full-text index's segments;
# other nights do a bounded merge.
set -euo pipefail
command -v zstd >/dev/null || { echo "::error title=Crawl database::zstd is not installed on this runner"; exit 1; }
exec python3 "$(dirname "$0")/ci_db.py" "$@"
