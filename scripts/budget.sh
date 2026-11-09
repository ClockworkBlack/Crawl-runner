#!/usr/bin/env bash
# The minutes an optional merge-job step may use: what it asks for, cut so the steps that save the run (the database,
# the bundle, state, the Electo import, the site) always keep RESERVE minutes before the job's limit. Prints 0 when
# there is no time left for it (the step then skips). MERGE_T0 (the job's start) and MERGE_LIMIT come from the
# job's first step.  Usage: M=$(bash scripts/budget.sh 25)
want=${1:-10}
limit=${MERGE_LIMIT:-350}
reserve=${MERGE_RESERVE:-75}
t0=${MERGE_T0:-$(date +%s)}
used=$(( ($(date +%s) - t0) / 60 ))
left=$(( limit - reserve - used ))
[ "$left" -lt 0 ] && left=0
[ "$want" -lt "$left" ] && echo "$want" || echo "$left"
