#!/usr/bin/env bash
# The nightly legis-votes job's hand-off to the crawl (gacrawl/legis_handoff.py says what is in it).
#   scripts/legis_handoff.sh carry     (legis-votes) a copy of the crawl database, with a hand-off the crawl has not
#                                      applied yet applied to it; LEGIS_SINCE (where this run's records start) to $GITHUB_ENV
#   scripts/legis_handoff.sh upload    (legis-votes) this run's records and marks to the private crawl-data release
#   scripts/legis_handoff.sh apply     (crawl merge job) the waiting hand-off applied to the main database; its asset
#                                      ids kept in legisd/applied
#   scripts/legis_handoff.sh cleanup   (crawl merge job, once the database is stored) the applied hand-off deleted, by
#                                      asset id: a newer one uploaded meanwhile stays for the next run
set -euo pipefail
TAG="${DATA_TAG:-crawl-data}"
REPO="${GH_REPO:-$GITHUB_REPOSITORY}"
assets() {   # "name=id=updated_at" for the hand-off's two files
  gh api "repos/$REPO/releases/tags/$TAG" --jq '[.assets[] | select(.name=="legis-delta.db.zst" or .name=="legis-kv.json") | "\(.name)=\(.id)=\(.updated_at)"] | join(" ")' 2>/dev/null || true
}
fetch() {    # $1 = directory; downloads the waiting hand-off there; prints the asset lines, nothing when none waits
  local A D K
  A=$(assets); D=$(echo "$A" | tr ' ' '\n' | grep '^legis-delta.db.zst=' || true); K=$(echo "$A" | tr ' ' '\n' | grep '^legis-kv.json=' || true)
  [ -n "$D" ] || [ -n "$K" ] || return 0
  mkdir -p "$1"
  if [ -n "$D" ]; then
    gh api -H 'Accept: application/octet-stream' "repos/$REPO/releases/assets/$(echo "$D" | cut -d= -f2)" > "$1/legis-delta.db.zst"
    zstd -d -q -f "$1/legis-delta.db.zst" -o "$1/legis-delta.db" && rm -f "$1/legis-delta.db.zst"
  fi
  if [ -n "$K" ]; then gh api -H 'Accept: application/octet-stream' "repos/$REPO/releases/assets/$(echo "$K" | cut -d= -f2)" > "$1/legis-kv.json"
  else echo "{}" > "$1/legis-kv.json"; fi
  echo "$D $K"
}
case "${1:-}" in
  carry)
    bash scripts/ci_db.sh pull
    SINCE=$(date -u +%Y-%m-%dT%H:%M:%S)
    W=$(fetch waiting)
    if [ -n "$W" ]; then
      gacrawl legis apply --delta waiting/legis-delta.db --kv waiting/legis-kv.json >/dev/null
      OLD=$(python3 -c "import json; print(json.load(open('waiting/legis-kv.json')).get('_since', ''))")
      [ -n "$OLD" ] && [ "$OLD" \< "$SINCE" ] && SINCE="$OLD"
      echo "a hand-off was still waiting for the crawl: carried (records since $SINCE)"
    fi
    rm -rf waiting
    echo "LEGIS_SINCE=$SINCE" >> "$GITHUB_ENV"
    ;;
  upload)
    : "${LEGIS_SINCE:?LEGIS_SINCE (where the records of this run start)}"
    gacrawl delta --since "$LEGIS_SINCE" --out legis-delta.db >/dev/null
    gacrawl legis export-kv --out legis-kv.json --since "$LEGIS_SINCE" >/dev/null
    n=$(python3 -c "import sqlite3; print(sqlite3.connect('legis-delta.db').execute('select count(*) from records').fetchone()[0])")
    zstd -T0 -3 -q -f legis-delta.db -o legis-delta.db.zst
    gh release upload "$TAG" legis-delta.db.zst legis-kv.json --clobber
    echo "handed to the crawl: $n records ($(du -h legis-delta.db.zst | cut -f1)) and the marks of every bill read"
    ;;
  apply)
    W=$(fetch legisd)
    if [ -z "$W" ]; then echo "no legislature votes hand-off waiting"; exit 0; fi
    gacrawl legis apply --delta legisd/legis-delta.db --kv legisd/legis-kv.json
    echo "$W" > legisd/applied
    ;;
  cleanup)
    [ -f legisd/applied ] || exit 0
    NOW=$(assets)
    for a in $(cat legisd/applied); do
      if echo " $NOW " | grep -q " $a "; then gh api -X DELETE "repos/$REPO/releases/assets/$(echo "$a" | cut -d= -f2)" >/dev/null || true
      else echo "a newer legislature votes hand-off arrived while this run worked: kept for the next run"; fi
    done
    rm -f legisd/applied; echo "the applied legislature votes hand-off is stored; removed"
    ;;
  *) echo "usage: $0 carry|upload|apply|cleanup"; exit 2 ;;
esac
