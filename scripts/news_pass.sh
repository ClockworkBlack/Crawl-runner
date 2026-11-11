#!/usr/bin/env bash
# One pass of the News loop (.github/workflows/news.yml runs gacrawl news --every 10 --then "bash scripts/news_pass.sh"):
# Electo's import of the digest (ga/crawl_import.py news_from: stories written by hand stay, and an outlet that did not
# answer this time keeps its stories), summaries by the open model for the new stories only (tools/ai/news_ai.py), then
# the commit and the public site (tools/site_data.sh), both only when the stories changed. Electo is cloned on the first
# pass and pulled after. A pass that fails says why and leaves the next pass to try again.
#   env: ELECTO_TOKEN ELECTO_REPO SITE_TOKEN SITE_DATA_REPO (NEWS_PASS: the pass's number, from gacrawl)
set -uo pipefail
BUNDLE="$PWD/dist/news"
E="${ELECTO_DIR:-/tmp/electo-news}"
P="news pass ${NEWS_PASS:-?}"
[ -s "$BUNDLE/news.json" ] || { echo "$P: no digest"; exit 0; }
if [ -d "$E/.git" ]; then
  git -C "$E" pull -q --rebase --autostash || { echo "$P: Electo could not be updated; a fresh copy next pass"; rm -rf "$E"; exit 0; }
else
  git clone -q --depth 1 "https://x-access-token:${ELECTO_TOKEN}@github.com/${ELECTO_REPO}.git" "$E" || { echo "$P: Electo could not be cloned"; exit 0; }
  git -C "$E" config user.name "gacrawl-bot"
  git -C "$E" config user.email "gacrawl-bot@users.noreply.github.com"
fi
cd "$E" || exit 0
python3 -I -c "import sys; sys.path.insert(0, 'ga'); import crawl_import as ci; ci.news_from(sys.argv[1])" "$BUNDLE" \
  || { echo "$P: the import failed"; git checkout -q -- ga/news.json; exit 0; }
# summaries for the stories that have none yet (the model loads only when there is one; a failure keeps the excerpts)
[ -f tools/ai/news_ai.py ] && timeout 9m python3 -I tools/ai/news_ai.py --text "$BUNDLE/news_text.json" --minutes 6 \
  || echo "$P: no model summaries"
for f in ga/news.json research/train/news_ai_log.jsonl; do [ -e "$f" ] && git add "$f"; done
if git diff --cached --quiet; then echo "$P: no new stories"; exit 0; fi
git commit -qm "News from Crawl: $(date -u +%Y-%m-%dT%H:%MZ)"
pushed=
for i in 1 2 3 4 5; do git pull --rebase --autostash -q && git push -q && { pushed=1; break; }; sleep $((i * 5)); done
[ -n "$pushed" ] || { echo "$P: the commit could not be pushed; the next pass tries again"; git reset -q --hard "@{u}"; exit 0; }
# the public site and data feed; the ordinances (their own workflow) are not checked on every pass
[ -f tools/site_data.sh ] && GH_TOKEN="$SITE_TOKEN" SKIP_ORDINANCES=1 timeout 15m bash tools/site_data.sh || echo "$P: site data not refreshed"
