#!/usr/bin/env bash
# Campaign money to Electo now: the Donors tab (ga/crawl/donors.json, every donor's profile in ga/crawl/donors/) and
# the money on each profile (ga/crawl/people.json), through Electo's money-only import (tools/money_import.py), so the
# money a side job reads (fec-reread, ga-money) is on the site within minutes instead of after the crawl's next merge.
#
# Built on a scratch copy of the newest stored crawl database (crawl.db.zst) with every money hand-off still waiting
# for the crawl applied on top (the FEC re-read's fec-delta.db.zst + fec-kv.json, the Georgia money job's
# ga-delta.db.zst + ga-kv.json) and the caller's own deltas (arguments, in case the crawl took the uploaded copy in
# between). A job pushing its own copy alone would take newer money of the other kind off the site (its copy is as old
# as its start); built this way, every push carries everything read so far, whichever job pushes. Donor ids are given
# on the scratch copy only to export it; nothing here is handed back, and the copy is removed. The page itself is
# rebuilt by publish-site (every 20 minutes, whenever Electo changes).
#
#   scripts/money_to_electo.sh "LABEL" [own-delta.db ...]
# needs DATA_TAG and gh's repository and token (scripts/ci_runner.sh mode), ELECTO_TOKEN, ELECTO_REPO
set -euo pipefail
LABEL="${1:-campaign money}"; shift || true
REPO="${GH_REPO:-$GITHUB_REPOSITORY}"
W="$PWD/.money-push"
rm -rf "$W"; mkdir -p "$W/h"
trap 'rm -rf "$W" /tmp/electo-money' EXIT
asset() { gh api "repos/$REPO/releases/tags/$DATA_TAG" --jq ".assets[] | select(.name==\"$1\") | .id" 2>/dev/null || true; }
get() { local id; id=$(asset "$1"); [ -n "$id" ] && gh api -H 'Accept: application/octet-stream' "repos/$REPO/releases/assets/$id" > "$2" && [ -s "$2" ]; }

T0=$(date +%s)
get crawl.db.zst "$W/crawl.db.zst" || { echo "money push: no stored database"; exit 1; }
zstd -d -q -f --rm "$W/crawl.db.zst" -o "$W/crawl.db"
export GACRAWL_DB="$W/crawl.db"
if get fec-delta.db.zst "$W/h/fec.db.zst"; then
  zstd -d -q -f --rm "$W/h/fec.db.zst" -o "$W/h/fec.db"
  get fec-kv.json "$W/h/fec-kv.json" || echo "{}" > "$W/h/fec-kv.json"
  gacrawl finance apply --delta "$W/h/fec.db" --kv "$W/h/fec-kv.json" | tail -1
fi
if get ga-delta.db.zst "$W/h/ga.db.zst"; then
  zstd -d -q -f --rm "$W/h/ga.db.zst" -o "$W/h/ga.db"
  gacrawl finance apply --delta "$W/h/ga.db" | tail -1
  if get ga-kv.json "$W/h/ga-kv.json"; then gacrawl ethics apply-kv --kv "$W/h/ga-kv.json" >/dev/null; fi
fi
for X in "$@"; do
  if [ -s "$X" ]; then gacrawl finance apply --delta "$X" | tail -1; fi
done
rm -rf "$W/h"
gacrawl finance link | tail -c 300; echo
gacrawl finance export-money --out "$W/money" | tail -2
echo "money push: built in $(( $(date +%s) - T0 )) s"
if [ -n "${MONEY_DRY_RUN:-}" ]; then
  rm -rf "$MONEY_DRY_RUN"; mv "$W/money" "$MONEY_DRY_RUN"; echo "money push: dry run, kept in $MONEY_DRY_RUN"; exit 0
fi

# Electo: a fresh clone each try (another writer may push in between: the import is made again on its newest commit)
for i in 1 2 3 4 5; do
  rm -rf /tmp/electo-money
  git clone -q --depth 1 "https://x-access-token:${ELECTO_TOKEN}@github.com/${ELECTO_REPO}.git" /tmp/electo-money
  (
    cd /tmp/electo-money
    CRAWL_BUNDLE="$W/money" python3 -I tools/money_import.py
    git config user.name "gacrawl-bot" && git config user.email "gacrawl-bot@users.noreply.github.com"
    git add -A ga/crawl/donors.json ga/crawl/donors ga/crawl/people.json
    if git diff --cached --quiet; then echo "money push: Electo already shows this money"; exit 0; fi
    git commit -qm "Campaign money ($LABEL): $(date -u +%Y-%m-%dT%H:%MZ)"
    git push -q
  ) && { echo "money push: on Electo ($LABEL) in $(( $(date +%s) - T0 )) s"; exit 0; }
  sleep $((i * 10))
done
echo "::warning title=Money push::Electo did not take the money push ($LABEL) after 5 tries; the crawl's next merge brings it"
exit 1
