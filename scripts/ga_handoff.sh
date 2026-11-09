#!/usr/bin/env bash
# The Georgia money job's hand-off to the crawl (ga-money.yml): its lines since GA_SINCE (ga-delta.db.zst) and where
# each full read stopped (ga-kv.json, with _since, so a run that carries it knows where it starts), in the private
# crawl-data release. The crawl's merge job applies it and deletes it by its asset id once stored.
set -euo pipefail
: "${GA_SINCE:?GA_SINCE (where this run's lines start)}"
gacrawl delta --since "$GA_SINCE" --key-like 'ga2|%' --out ga-delta.db >/dev/null
gacrawl ethics export-kv --kv ga-kv.json >/dev/null
python3 - <<'PY'
import json, os
d = json.load(open("ga-kv.json"))
d["_since"] = os.environ["GA_SINCE"]
json.dump(d, open("ga-kv.json", "w"))
PY
zstd -T0 -3 -q -f ga-delta.db -o ga-delta.db.zst
gh release upload "$DATA_TAG" ga-delta.db.zst ga-kv.json --clobber
echo "uploaded $(du -h ga-delta.db.zst | cut -f1) of Georgia lines for the crawl's next merge"
