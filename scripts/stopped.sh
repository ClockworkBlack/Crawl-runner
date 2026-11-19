#!/usr/bin/env bash
# Tell a step stopped by its time limit from one that crashed.
#
#   some-command ... || bash "$GITHUB_WORKSPACE/scripts/stopped.sh" $? "what stopped after N minutes" [fatal]
#
# `|| echo "… stopped after N minutes"` reported every non-zero exit as a time-out: four merge jobs from Nov 14 to 18
# showed a traceback (gacrawl.fetch.OutOfTime raised uncaught) a minute into the legislature step, then "the
# legislature step stopped after 6 minutes", and the step stayed green. Here:
#   124 (timeout's own), 130 / 143 (SIGINT / SIGTERM), 137 (killed after timeout's -k grace)  -> the message, exit 0
#   anything else -> a ::warning:: naming the exit code (exit 0), or with "fatal" an ::error:: and that exit code
rc="${1:-1}"; msg="${2:-the command stopped}"; mode="${3:-warn}"
case "$rc" in
  0) exit 0 ;;
  124|130|137|143) echo "$msg (exit $rc: the time limit)"; exit 0 ;;
esac
if [ "$mode" = "fatal" ]; then
  echo "::error::ended with exit $rc, an error and not the time limit (where a time-out would read: $msg); see the lines above"
  exit "$rc"
fi
echo "::warning::ended with exit $rc, an error and not the time limit (where a time-out would read: $msg); see the lines above"
exit 0
