#!/usr/bin/env bash
# How many parallel jobs a workflow runs: its Pro size or its free-plan size.
#   bash scripts/shards.sh PRO FREE        prints PRO, or FREE when the account is at its limit on jobs at once
#
# The account is on GitHub Pro (40 jobs at once) since Nov 2026; on the free plan it is 20. When the plan ends nothing
# says so, but jobs start waiting in line for a slot: a job of this repository queued for more than three minutes
# means the account is full, and then every workflow sizes itself for the free plan. A full account on Pro does the
# same for that one run, which is what it should do.
# SHARDS_PLAN=pro|free (a repository variable passed in by the workflow) settles it by hand.
# Needs a token that can read this repository's Actions (RUNNER_TOKEN, else GH_TOKEN).
set -uo pipefail
PRO=${1:?pro size}; FREE=${2:?free size}
case "${SHARDS_PLAN:-auto}" in pro) echo "$PRO"; exit 0;; free) echo "$FREE"; exit 0;; esac
export GH_TOKEN="${RUNNER_TOKEN:-${GH_TOKEN:-}}"
REPO="${GITHUB_REPOSITORY:-}"
[ -n "$REPO" ] && [ -n "$GH_TOKEN" ] || { echo "$PRO"; exit 0; }
n=0
for id in $( (gh api "repos/$REPO/actions/runs?status=queued&per_page=100" --jq '.workflow_runs[].id';
              gh api "repos/$REPO/actions/runs?status=in_progress&per_page=100" --jq '.workflow_runs[].id') 2>/dev/null); do
  [[ "$id" =~ ^[0-9]+$ ]] && [ "$id" != "${GITHUB_RUN_ID:-}" ] || continue
  c=$(gh api "repos/$REPO/actions/runs/$id/jobs?per_page=100" --jq \
      '[.jobs[] | select(.status=="queued" and ((now - (.created_at | fromdateiso8601)) > 180))] | length' 2>/dev/null) || c=0
  [[ "$c" =~ ^[0-9]+$ ]] || c=0
  n=$((n + c))
done
if [ "$n" -gt 0 ]; then
  echo "::warning::$n job(s) waiting for a slot: the account is full; sized for the free plan ($FREE, not $PRO)" >&2
  echo "$FREE"
else
  echo "$PRO"
fi
