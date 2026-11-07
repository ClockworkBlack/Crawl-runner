#!/usr/bin/env bash
# Set up this machine as a self-hosted GitHub Actions runner for the crawl (Ubuntu/Debian; a Raspberry Pi or an
# old laptop on a home connection is enough). Why: GitHub's own runners are Azure machines, and 69 official sites
# (CivicPlus / Granicus firewalls) refuse data-center address ranges; a runner on an ordinary connection reaches
# them with the same identification, robots.txt and spacing rules, and costs no Actions minutes.
#
#   bash scripts/selfhosted.sh            check and install what the crawl needs, then print the registration steps
#
# Registration needs a token from the repository's Settings > Actions > Runners > "New self-hosted runner"; this
# script never asks for it and never sees it: you paste it into the runner's own config.sh (printed below).
set -euo pipefail
REPO="${REPO:-ClockworkBlack/Crawl}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner}"

need() { command -v "$1" >/dev/null 2>&1; }
echo "== what the crawl needs on this machine"
MISSING=()
need python3 || MISSING+=(python3)
python3 - <<'PY' || MISSING+=(python3.11+)
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
need git || MISSING+=(git)
need zstd || MISSING+=(zstd)
need gh || MISSING+=(gh)
need tesseract || echo "   tesseract-ocr: optional (scanned documents are read in the merge job; install it if MERGE_RUNNER points here)"
need google-chrome || need chromium || need chromium-browser || echo "   a browser: optional (Playwright downloads its own Chromium on the first run)"
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "   missing: ${MISSING[*]}"
  echo "   Ubuntu/Debian: sudo apt-get install -y python3 python3-venv python3-pip git zstd"
  echo "   gh (GitHub CLI): https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
  echo "   then run this script again"
  exit 1
fi
echo "   python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'), git, zstd, gh: ok"

echo
echo "== the runner's own connection (the point of a home runner)"
UA="GACivicCrawler/0.3 (+https://github.com/$REPO)"
for u in https://www.atlantaga.gov/robots.txt https://www.cherokeega.com/robots.txt https://www.smyrnaga.gov/robots.txt; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" --max-time 20 "$u" || echo "000")
  echo "   $code  $u"
done
echo "   (200 here, where GitHub's runners get 403, means the block is on the address range, not the crawler)"

echo
echo "== register the runner (needs a token only you can see)"
echo "   1. Open https://github.com/$REPO/settings/actions/runners/new?arch=$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/')&os=linux"
echo "      and copy the commands it shows: they download the runner into a folder (say $RUNNER_DIR) and run"
echo "      ./config.sh --url https://github.com/$REPO --token <the token on that page>"
echo "      Accept the default labels (self-hosted, Linux, $(uname -m)); name it after the machine."
echo "   2. Run it as a service so it survives reboots:"
echo "      cd $RUNNER_DIR && sudo ./svc.sh install && sudo ./svc.sh start"
echo "   3. In the repository's Settings > Secrets and variables > Actions > Variables, add"
echo "      CRAWL_RUNNER = self-hosted        (the crawl shards run here: pages, documents, the browser)"
echo "      MERGE_RUNNER = self-hosted        (optional: research, finance and Code changes too)"
echo "      Delete a variable to go back to GitHub's runners. Nothing else changes: the same rules, budgets and"
echo "      release assets; the runner only needs to be on and online at the run's hour (nightly 04:07 UTC,"
echo "      the daytime backfill from 06:00 ET). A run that finds the runner offline waits for it, so set"
echo "      the variable only while the machine stays on."
echo "   The runner runs only this repository's workflows (a private repository: nobody else can send it work)."
