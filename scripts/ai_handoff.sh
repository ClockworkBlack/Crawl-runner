#!/usr/bin/env bash
# The AI jobs' shard results, passed from the shard jobs to the combine job through the PRIVATE data repository's
# release (config/runner.json data_repo; DATA_TAG, crawl-data: the release the runner already hands the crawl its
# FEC, Georgia money, legislature and model hand-offs through), never as run attachments: the runner repository is
# public, and anyone can download a public repository's attachments (model output about named people, before the
# checker and the combine, for up to a day; until Nov 19 they went up as plain attachments).
#
#   ai_handoff.sh put KEY PART FILE..   each FILE as asset ai-KEY-<run id>-PART--<file name> (KEY: the workflow)
#   ai_handoff.sh get KEY DIR           this run's assets of KEY into DIR/<PART>/<file name> (the layout
#                                       download-artifact gave); their ids are noted in DIR/.assets
#   ai_handoff.sh drop DIR              delete the assets noted in DIR/.assets, by id (once the results are saved)
#   ai_handoff.sh clean [DAYS]          delete this script's assets older than DAYS (3): a run whose combine failed leaves its own
#
# Needs DATA_TOKEN (the private repository's token); DATA_REPO is read from config/runner.json when not set.
set -euo pipefail
cmd="${1:-}"; shift || true
TAG="${DATA_TAG:-crawl-data}"
[ -n "${DATA_TOKEN:-}" ] || { echo "::error::ai_handoff.sh: no DATA_TOKEN; the shard results cannot be passed on"; exit 1; }
export GH_TOKEN="$DATA_TOKEN"
if [ -z "${DATA_REPO:-}" ]; then
  DATA_REPO=$(python3 -c "import json;print(json.load(open('config/runner.json')).get('data_repo') or '')" 2>/dev/null || true)
fi
[ -n "$DATA_REPO" ] || { echo "::error::ai_handoff.sh: no data repository (config/runner.json data_repo)"; exit 1; }
RUN="${GITHUB_RUN_ID:?}"

release_id() { gh api "repos/$DATA_REPO/releases/tags/$TAG" --jq .id 2>/dev/null || true; }
assets() {   # id name updated_at, every asset of the release
  local rid="$1"
  gh api --paginate "repos/$DATA_REPO/releases/$rid/assets?per_page=100" --jq '.[] | "\(.id) \(.name) \(.updated_at)"'
}
safe() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }

case "$cmd" in
  put)
    key=$(safe "$1"); part=$(safe "$2"); shift 2
    rid=$(release_id)
    # the runner's own hand-off release, which the crawl made and every hand-off job writes to: never created here
    [ -n "$rid" ] || { echo "::error::no $TAG release in $DATA_REPO (the crawl's data release); the shard results cannot be passed on"; exit 1; }
    d=$(mktemp -d)
    for f in "$@"; do
      [ -f "$f" ] || { echo "$f: not there, not passed on"; continue; }
      n="ai-$key-$RUN-$part--$(safe "$(basename "$f")")"
      cp "$f" "$d/$n"
      ok=false
      for i in 1 2 3; do gh release upload "$TAG" "$d/$n" -R "$DATA_REPO" --clobber && { ok=true; break; }; sleep $((i * 10)); done
      [ "$ok" = true ] || { echo "::error::$n could not be uploaded"; rm -rf "$d"; exit 1; }
      echo "passed on: $n ($(du -h "$f" | cut -f1))"
    done
    rm -rf "$d"
    ;;
  get)
    key=$(safe "$1"); dir="$2"; mkdir -p "$dir"; : > "$dir/.assets"
    rid=$(release_id); [ -n "$rid" ] || { echo "no $TAG release: nothing passed on"; exit 0; }
    pre="ai-$key-$RUN-"; n=0
    while read -r id name at; do
      case "$name" in "$pre"*--*) ;; *) continue ;; esac
      rest="${name#"$pre"}"; part="${rest%%--*}"; file="${rest#*--}"
      mkdir -p "$dir/$part"
      gh api -H 'Accept: application/octet-stream' "repos/$DATA_REPO/releases/assets/$id" > "$dir/$part/$file"
      echo "$id $name" >> "$dir/.assets"; n=$((n + 1))
    done < <(assets "$rid")
    echo "$n shard file(s) of this run fetched into $dir"
    ;;
  drop)
    dir="$1"; [ -s "$dir/.assets" ] || { echo "nothing to remove"; exit 0; }
    while read -r id name; do
      gh api -X DELETE "repos/$DATA_REPO/releases/assets/$id" >/dev/null && echo "removed $name" || echo "::warning::$name not removed (the cleanup takes it later)"
    done < "$dir/.assets"
    ;;
  clean)
    days="${1:-3}"; rid=$(release_id); [ -n "$rid" ] || exit 0
    cut=$(date -u -d "$days days ago" +%Y-%m-%dT%H:%M:%SZ)
    while read -r id name at; do
      # only this script's own names (ai-KEY-<run id>-PART--file): the release holds the crawl's other hand-offs too
      [[ "$name" =~ ^ai-[A-Za-z0-9._-]+-[0-9]+-[A-Za-z0-9._-]+--.+ ]] || continue
      if [[ "$at" < "$cut" ]]; then
        gh api -X DELETE "repos/$DATA_REPO/releases/assets/$id" >/dev/null && echo "removed $name (left since $at)" || true
      fi
    done < <(assets "$rid")
    ;;
  *) echo "usage: ai_handoff.sh put KEY PART FILE.. | get KEY DIR | drop DIR | clean [DAYS]"; exit 2 ;;
esac
