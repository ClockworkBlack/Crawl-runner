#!/usr/bin/env bash
# The minutes an optional merge-job step may use: what it asks for, cut so the steps that save the run (the late money
# hand-offs, the database, the bundle, state, the Electo import, the site) always keep RESERVE minutes before the job's
# limit. Prints 0 when there is no time left for it (the step then skips), and always 0 in an export-only run
# (`job: export-only`: store, export and import what the database holds, nothing else). MERGE_T0 (the job's start) and
# MERGE_LIMIT come from the job's first step.  Usage: M=$(bash scripts/budget.sh 25)
# RESERVE: 110 since Nov 13, 2026 (was 75). The saving steps took about 45 minutes on Nov 13 (late money 8-14, the
# export 17, storing 6, the Electo push and import 11), and a run whose research used everything up to the old reserve
# reached the export with too little left: the late money step is not optional and took 14 of the 75.
want=${1:-10}
if [ "${EXPORT_ONLY:-false}" = "true" ]; then echo 0; exit 0; fi
limit=${MERGE_LIMIT:-350}
reserve=${MERGE_RESERVE:-110}
t0=${MERGE_T0:-$(date +%s)}
used=$(( ($(date +%s) - t0) / 60 ))
left=$(( limit - reserve - used ))
[ "$left" -lt 0 ] && left=0
[ "$want" -lt "$left" ] && echo "$want" || echo "$left"
