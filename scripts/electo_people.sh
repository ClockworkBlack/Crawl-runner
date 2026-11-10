#!/usr/bin/env bash
# state/electo_people.json for a job that researches or reads the candidates (gacrawl/candidates.py load_candidates):
# everyone on a Georgia ballot since 2016, imported from a sparse copy of Electo (`gacrawl electo import`). A job
# without it sees only the people listed in config/candidates.yaml (2,881 of about 7,900): until Nov 9 the crawl's
# merge job, the candidates' news job and the FEC re-read all ran that way.
#   scripts/electo_people.sh          (needs ELECTO_TOKEN or DATA_TOKEN; ELECTO_REPO optional)
set -euo pipefail
if [ -s state/electo_people.json ]; then echo "Electo people: already here"; exit 0; fi
TOK="${ELECTO_TOKEN:-${DATA_TOKEN:-}}"; REPO="${ELECTO_REPO:-ClockworkBlack/Electo}"
if [ -z "$TOK" ]; then echo "::warning title=Electo people::no token to read Electo: research covers config/candidates.yaml only"; exit 0; fi
rm -rf .electo
git clone -q --depth 1 --filter=blob:none --sparse "https://x-access-token:${TOK}@github.com/${REPO}.git" .electo
git -C .electo sparse-checkout set --no-cone /ga/pipeline_archive/people.json /ga/pipeline_archive/aliases.json \
  /ga/pipeline_archive/splits.json /ga/pipeline_archive/profiles.json /ga/pipeline_archive/names.json \
  /ga/pipeline_archive/extra.json /ga/pipeline_archive/crawl_elections.json /ga/profiles.json /ga/profiles_local.json \
  /ga/councils.json /ga/localcodes.json
gacrawl electo import --path .electo | tail -4
rm -rf .electo
