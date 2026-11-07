#!/usr/bin/env bash
# Public runner / private data (config/runner.json).
#
# The crawler's code also lives in a PUBLIC repository (runner_repo) whose Actions minutes are free; everything it
# gathers lives in this PRIVATE repository (data_repo): the progress files under state/, the records-request
# tracker and received mail, the database and the Electo bundle (release assets). When runner.json is active:
#   - in the public repository, every job reads its state from the private one and writes it back there, release
#     assets go to the private repository's releases, and anything passed between jobs is encrypted with the
#     DATA_TOKEN secret (a public repository's run attachments can be downloaded by anyone)
#   - in the private repository, scheduled runs stand down (the public one runs them); a run started by hand still
#     works here
# Inactive, or anywhere else: everything works as before, in this repository.
#
#   ci_runner.sh mode              export DATA_REPO / PUBLIC_RUNNER / STAND_DOWN / GH_REPO / GH_TOKEN to $GITHUB_ENV
#   ci_runner.sh pull-state        public runner: copy the private repository's kept files into this checkout
#   ci_runner.sh pack NAME PATH..  bundle paths into upload/NAME (encrypted on the public runner)
#   ci_runner.sh unpack FILE..     restore what pack bundled
#   ci_runner.sh push-state MSG PATH..   commit the kept files: to the private repository from the public runner,
#                                        else to this repository
set -euo pipefail
cmd="${1:-}"; shift || true
CFG=config/runner.json

json() { python3 -c "import json,sys;d=json.load(open('$CFG'));print(d.get(sys.argv[1]) or '')" "$1" 2>/dev/null || true; }

case "$cmd" in
  mode)
    active=$(json active); runner=$(json runner_repo); data=$(json data_repo)
    public=false; stand=false
    if [ "$active" = "True" ] && [ -n "$runner" ] && [ -n "$data" ]; then
      if [ "${GITHUB_REPOSITORY,,}" = "${runner,,}" ]; then
        public=true
        [ -n "${DATA_TOKEN:-}" ] || { echo "::error::public runner without the DATA_TOKEN secret: nothing can be stored"; exit 1; }
      elif [ "${GITHUB_REPOSITORY,,}" = "${data,,}" ] && [ "${GITHUB_EVENT_NAME:-}" = "schedule" ]; then
        stand=true
      fi
    fi
    {
      echo "PUBLIC_RUNNER=$public"
      echo "STAND_DOWN=$stand"
      if [ "$public" = "true" ]; then
        echo "DATA_REPO=$data"
        echo "GH_REPO=$data"                 # gh release / gh api default to the private repository
        echo "GH_TOKEN=$DATA_TOKEN"
        echo "GACRAWL_ISSUES_REPO=$data"     # records-request deadline issues are opened there, not in public
      fi
    } >> "$GITHUB_ENV"
    echo "runner mode: public=$public stand_down=$stand"
    ;;
  pull-state)
    [ "${PUBLIC_RUNNER:-false}" = "true" ] || exit 0
    rm -rf .data
    git clone -q --depth 1 --filter=blob:none --sparse "https://x-access-token:${DATA_TOKEN}@github.com/${DATA_REPO}.git" .data
    git -C .data sparse-checkout set --no-cone /state/ /imports/screens/ /data/openrecords/ /data/verified/ /data/rumor_mill/ \
      /data/manual_tasks.md /data/seed_gaps.md /config/private/
    for p in config/private state imports/screens data/openrecords data/verified data/rumor_mill data/manual_tasks.md data/seed_gaps.md; do
      if [ -e ".data/$p" ]; then mkdir -p "$(dirname "$p")"; rm -rf "$p"; cp -r ".data/$p" "$p"; fi
    done
    rm -rf .data
    echo "state from $DATA_REPO: $(ls state 2>/dev/null | wc -l) files"
    ;;
  pack)
    name="$1"; shift
    mkdir -p upload
    have=(); for p in "$@"; do [ -e "$p" ] && have+=("$p"); done
    [ ${#have[@]} -gt 0 ] || have=(.)  # never an empty bundle
    if [ "${PUBLIC_RUNNER:-false}" = "true" ]; then
      tar -cf - "${have[@]}" | gzip -1 | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:DATA_TOKEN -out "upload/$name.enc"
    else
      tar -czf "upload/$name.tar.gz" "${have[@]}"
    fi
    ;;
  unpack)
    for f in "$@"; do
      case "$f" in
        *.enc) openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:DATA_TOKEN -in "$f" | tar -xzf - ;;
        *.tar.gz) tar -xzf "$f" ;;
      esac
    done
    ;;
  push-state)
    msg="$1"; shift
    if [ "${PUBLIC_RUNNER:-false}" = "true" ]; then
      rm -rf .data
      git clone -q --depth 1 "https://x-access-token:${DATA_TOKEN}@github.com/${DATA_REPO}.git" .data
      for p in "$@"; do
        [ -e "$p" ] || continue
        mkdir -p ".data/$(dirname "$p")"; rm -rf ".data/$p"; cp -r "$p" ".data/$p"
      done
      cd .data
    fi
    git config user.name "gacrawl-bot"; git config user.email "gacrawl-bot@users.noreply.github.com"
    for p in "$@"; do if [ -e "$p" ]; then git add -A "$p"; fi; done
    git diff --cached --quiet && { echo "nothing new to keep"; exit 0; }
    git commit -qm "$msg"
    for i in 1 2 3 4 5; do git pull --rebase --autostash -q && git push -q && { echo "kept in ${DATA_REPO:-this repository}"; exit 0; }; sleep $((i*5)); done
    exit 1
    ;;
  *) echo "usage: ci_runner.sh mode|pull-state|pack|unpack|push-state"; exit 2 ;;
esac
