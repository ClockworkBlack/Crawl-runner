#!/usr/bin/env bash
# The FEC re-read's hand-off to the crawl (fec-reread.yml): every line it read or changed since FEC_SINCE as a delta
# database, and its lookups and cursors (fec-kv.json, with _since, so a run that carries it knows where it starts),
# in the private crawl-data release. The crawl's merge job applies it and deletes it by its asset id once stored.
set -euo pipefail
: "${FEC_SINCE:?FEC_SINCE (where this run's lines start)}"
gacrawl delta --since "$FEC_SINCE" --out fec-delta.db >/dev/null
gacrawl finance export-kv --kv fec-kv.json
python3 - <<'PY'
import json, os
d = json.load(open("fec-kv.json"))
d["_since"] = os.environ["FEC_SINCE"]
json.dump(d, open("fec-kv.json", "w"))
PY
zstd -T0 -3 -q -f fec-delta.db -o fec-delta.db.zst
gh release upload "$DATA_TAG" fec-delta.db.zst fec-kv.json --clobber
echo "uploaded $(du -h fec-delta.db.zst | cut -f1) of re-read FEC lines for the crawl's next merge"
