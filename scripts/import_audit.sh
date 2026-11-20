#!/usr/bin/env bash
# Electo's database audit at the end of the crawl's import, run in the clone of Electo the import worked in (the
# current directory), AFTER ga/names.py. Called by crawl.yml's "Import into Electo" step.
#
# Why again: Electo's importer runs the audit at its own end (ga/crawl_import.py), before ga/names.py has written this
# import's record-only people into the name registry the audit reads. The file it left (ga/crawl/data_audit.json, which
# the import commits) therefore called everyone new in this import unknown: on Nov 20, 2026 it listed 4 errors (278
# people, 307 council members and 26 pairs "nobody the site knows") where the same audit, run on the same commit by
# grand-cross minutes later, found the 1 that was real. The file is what grand-cross's error sends a reader to.
#
# Prints each failed check with its count, without its examples (this is the public runner's log; the examples are in
# the file, in the private repository), then the verdict. Always exits 0: an audit that finds errors does not stop the
# import (grand-cross fails on them, and LogView files that under data-audit); one that did not run to its end leaves
# the importer's own file and says so in a warning.
set -uo pipefail
[ -f tools/data_audit.py ] || { echo "no database audit in this Electo"; exit 0; }
log=$(mktemp); rc=0
timeout "${IMPORT_AUDIT_TIMEOUT:-5m}" python3 -I tools/data_audit.py > "$log" 2>&1 || rc=$?
if grep -q '^data audit: ' "$log"; then
  { grep -E '^[[:space:]]*ERROR\b' "$log" || true; } | sed -E 's/ e\.g\. .*$//' | cut -c1-300
  grep '^data audit: ' "$log" | tail -n 1 | cut -c1-200
else
  last=$(grep -v '^[[:space:]]*$' "$log" | tail -n 1 | cut -c1-240)
  echo "::warning title=Electo import::the database audit did not run to its end after the name registry (exit $rc): ${last:-no output}; the importer's own audit file stays"
  # a file cut short by the stop is not kept: back to the one Electo's main has
  if [ -e ga/crawl/data_audit.json ] && ! python3 -I -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" ga/crawl/data_audit.json 2>/dev/null; then
    git checkout -q HEAD -- ga/crawl/data_audit.json 2>/dev/null || rm -f ga/crawl/data_audit.json
  fi
fi
rm -f "$log"
exit 0
