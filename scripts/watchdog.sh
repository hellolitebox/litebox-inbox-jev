#!/usr/bin/env bash
# Staleness watchdog, run every 30 minutes by inbox-jev-watchdog.timer:
#
# 1. Alert in Slack when no triage pass finished successfully in the last
#    WATCHDOG_MAX_AGE_MINUTES (default 60): the timer stopped (a deploy left with --hold, a
#    disabled timer), passes hang until their timeout, or every pass fails. Once per incident
#    (a state file), then "recovered" when a pass succeeds again. Not while scripts/alert.sh's
#    alert about a failed pass is younger than ALERT_REPEAT_HOURS (default 6): that alert already
#    covers the incident; past that window the watchdog reminds once.
# 2. For every unit scripts/alert.sh alerted about, post "recovered" once its last run
#    succeeded, and forget the alert so the next failure alerts again. The watchdog's own alert
#    is recovered by this run reaching that point.
#
# A successful pass is systemd's "Finished" job message for the service (JOB_RESULT=done): a
# failed run logs "Failed to start" instead. Alerts are off (logged) without ALERT_SLACK_CHANNEL.
set -euo pipefail

# shellcheck source=ops_common.sh
. "$(dirname "$0")/ops_common.sh"

unit=inbox-jev-triage.service
max_age=$(env_value WATCHDOG_MAX_AGE_MINUTES 60)
require_count WATCHDOG_MAX_AGE_MINUTES "$max_age" 15
repeat_hours=$(env_value ALERT_REPEAT_HOURS 6)
require_count ALERT_REPEAT_HOURS "$repeat_hours" 1
self=inbox-jev-watchdog.service
state=$(state_dir)
host=$(hostname)
stale="$state/stale-$unit"

# systemd's "unit finished" message id (sd-messages.h: SD_MESSAGE_UNIT_STARTED).
# A journal that can't be read fails the script (and alerts), rather than looking stale.
messages=$(journalctl -q -o cat --no-pager --since "-${max_age}min" _PID=1 "UNIT=$unit" \
  MESSAGE_ID=39f53479d3a045ac8e11786248231fbf JOB_RESULT=done)
finished=$(printf '%s' "$messages" | grep -c . || true)

if [ "$finished" -gt 0 ]; then
  if [ -f "$stale" ]; then
    slack_post ":white_check_mark: *inbox-jev: triage passes succeed again* on \`$host\` (stale alert at $(date -r "$stale" '+%F %T %Z'))"
    rm -f "$stale"
  fi
  echo "ok: $finished successful triage passes in the last $max_age min"
elif [ -f "$stale" ]; then
  echo "still stale; already alerted at $(date -r "$stale" '+%F %T %Z')"
elif failed=$(find "$state/alerted-$unit" -mmin -"$((repeat_hours * 60))" 2>/dev/null) &&
  [ -n "$failed" ]; then
  # alert.sh posted this pass's failure, and repeats it after ALERT_REPEAT_HOURS: no second
  # alert for the same incident.
  echo "stale; a failed pass was already alerted at $(date -r "$failed" '+%F %T %Z')"
else
  timer=$(systemctl is-active inbox-jev-triage.timer || true)
  show() { systemctl show -p "$1" --value "$unit"; }
  slack_post ":warning: *inbox-jev: no successful triage pass in the last $max_age min* on \`$host\`
Timer: $timer. Service: $(show ActiveState), last result $(show Result), exit status $(show ExecMainStatus), last exit $(show ExecMainExitTimestamp).
Check: \`systemctl status inbox-jev-triage.timer inbox-jev-triage.service\`, \`journalctl -u $unit -n 200\`"
  if [ -n "$(env_value ALERT_SLACK_CHANNEL)" ]; then
    touch "$stale"
  fi
fi

# Recoveries of units scripts/alert.sh alerted about.
for marker in "$state"/alerted-*; do
  [ -e "$marker" ] || continue
  failed_unit=${marker##*/alerted-}
  if [ "$failed_unit" = "$self" ]; then
    # This is that unit, running: systemd has already reset its Result and exit timestamp, so
    # reaching this point is its recovery.
    slack_post ":white_check_mark: *inbox-jev: $failed_unit succeeds again* on \`$host\` (last alert $(date -r "$marker" '+%F %T %Z'))"
    rm -f "$marker"
    continue
  fi
  # Its last run succeeded and ended after the alert (not merely `systemctl reset-failed`).
  exited=$(systemctl show --timestamp=unix -p ExecMainExitTimestamp --value "$failed_unit")
  exited=${exited#@}
  case $exited in '' | *[!0-9]*) exited=0 ;; esac
  if [ "$(systemctl show -p Result --value "$failed_unit")" = success ] &&
    [ "$exited" -gt "$(date -r "$marker" +%s)" ]; then
    slack_post ":white_check_mark: *inbox-jev: $failed_unit succeeds again* on \`$host\` (last alert $(date -r "$marker" '+%F %T %Z'))"
    rm -f "$marker"
  fi
done
