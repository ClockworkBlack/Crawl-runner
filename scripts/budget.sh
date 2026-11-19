#!/usr/bin/env bash
# The minutes a merge-job step may use: what it asks for, cut to what the job's clock leaves. MERGE_T0 (the job's start)
# and MERGE_LIMIT come from the job's first step. Prints 0 when there is no time left for it (the step then skips).
#   M=$(bash scripts/budget.sh 25 [KEEP])        an optional step: cut so the steps that save the run (the late money
#        hand-offs, the database, the export and bundle, state, the Electo import, the site) keep RESERVE minutes
#        before the job's limit; always 0 in an export-only run (`job: export-only`: store, export and import what the
#        database holds, nothing else)
#   M=$(bash scripts/budget.sh --saving 12 money)  a saving step that can stop short and leave the rest to a later run
#        (`money`: the late money hand-offs, applied in full by the next run at its start; `export`: the export, whose
#        run then queues an export-only run): cut so the saving steps after it keep their slowest minutes (AFTER_*
#        below). In every run, an export-only one too
# RESERVE: 135 since Nov 19, 2026 (110 from Nov 13, 75 before). Run 44497934401 reached its saving steps at minute
# 256, the late money took 23 minutes (21 of them applying the FEC hand-off), and the export stopped at its own
# 45-minute limit while it wrote its last files, so nothing was imported into Electo; with a longer export limit the
# job's 350 minutes would have stopped the import instead. The reserve is each saving step at the most it took in the
# 32 merge jobs with an export from Nov 10 to Nov 19, 2026 (tests/test_crawl_budget.py holds the table), plus 10
# minutes for the last optional step running past the clock (16 on Nov 19, 10 of them the records contacts, off the
# clock near the end until then; they now come before research, on the clock, where they keep their 10 minutes):
DB=4             # storing the database (3.7 on Nov 19: 1.9 GB in two parts)
MONEY=5          # the late money: its linking took 3.7 at most, its apply one pass now; it gets up to 12 with room
POLICE=1         # the police job's newest files, swap for the export (seconds)
EXPORT=50        # the export: 46 on Nov 19 (stopped at 45), 16 to 34 in the 20 runs before it
# after the export: storing the bundle 6 (5.5), the summary and backfill 1, coverage 6 (5.6), the review decisions and
# the state 2 (1.3), the bundle copy for Electo 3 (2.2), the packets push 11 (10.4), the import 28 (28.1: three tries,
# Nov 16), the site data 7 (6.6), the cleanup and the job's end 1 (0.9)
AFTER_EXPORT=$(( 6 + 1 + 6 + 2 + 3 + 11 + 28 + 7 + 1 ))        # 65
AFTER_MONEY=$(( POLICE + EXPORT + AFTER_EXPORT ))               # 116
RESERVE=$(( 10 + DB + MONEY + AFTER_MONEY ))                     # 135
# KEEP (second argument, optional): minutes this step must leave for the optional steps after it. Candidate research
# passes it: asking for 200 minutes, it took everything the job had left in the daytime runs from Nov. 14, 2026, and the
# nine steps after it (the legislature, bill reasons, discovery, the acts' Code changes, federal bills, campaign finance,
# Georgia money, the organizations, meeting videos) found 0 minutes in up to 16 of 18 runs. Those minutes go to the steps
# after it in their order, so what must not wait comes first: the money steps (finance, Georgia money, the organizations)
# right after research, since behind bill reasons and the Code changes the Georgia step still found 0 minutes on Nov 19
# (tests/test_crawl_budget.py checks that it keeps its minutes even when every step before it uses all of its own).
limit=${MERGE_LIMIT:-350}
t0=${MERGE_T0:-$(date +%s)}
used=$(( ($(date +%s) - t0) / 60 ))
if [ "${1:-}" = "--saving" ]; then
  want=${2:-10}
  case "${3:-}" in
    money) need=$AFTER_MONEY ;;
    export) need=$AFTER_EXPORT ;;
    *) echo "budget.sh --saving: name the step (money or export)" >&2; exit 2 ;;
  esac
  left=$(( limit - used - need ))
else
  want=${1:-10}
  keep=${2:-0}
  if [ "${EXPORT_ONLY:-false}" = "true" ]; then echo 0; exit 0; fi
  reserve=${MERGE_RESERVE:-$RESERVE}
  left=$(( limit - reserve - used - keep ))
fi
[ "$left" -lt 0 ] && left=0
[ "$want" -lt "$left" ] && echo "$want" || echo "$left"
