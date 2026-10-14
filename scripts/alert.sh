#!/usr/bin/env bash
# Post a Slack alert that a systemd unit failed. Run by inbox-jev-alert@<unit>.service, which the
# triage, digest, backup and watchdog services name in OnFailure=; by hand:
#   scripts/alert.sh inbox-jev-triage.service
#
# The message has the unit, host, time, the unit's result and exit status, and the last
# ALERT_LOG_LINES (default 20) app log lines of the failed run, reduced by ops_message.py to
# level, msg (the app's loggers only), account, run_id, run counts and the exception's type: never
# a traceback's message, a library's log message or anything else that could quote an email. The
# full logs stay in the journal.
#
# One alert per unit per incident: while the unit keeps failing, it isn't repeated for
# ALERT_REPEAT_HOURS (default 6). scripts/watchdog.sh posts "recovered" once it succeeds again.
# Alerts are off (logged, exit 0) when ALERT_SLACK_CHANNEL isn't set.
set -euo pipefail

# shellcheck source=ops_common.sh
. "$(dirname "$0")/ops_common.sh"

unit=${1:?usage: $0 <unit>}
repeat_hours=$(env_value ALERT_REPEAT_HOURS 6)
require_count ALERT_REPEAT_HOURS "$repeat_hours" 1
log_lines=$(env_value ALERT_LOG_LINES 20)
require_count ALERT_LOG_LINES "$log_lines" 0

marker="$(state_dir)/alerted-$unit"
if [ -n "$(find "$marker" -mmin -"$((repeat_hours * 60))" 2>/dev/null)" ]; then
  echo "$unit failed again; already alerted at $(date -r "$marker" '+%F %T %Z'), not repeated"
  exit 0
fi

# systemd ≥ 249 passes the failed run's details to OnFailure= units; ask it when run by hand.
show() { systemctl show -p "$1" --value "$unit"; }
result=${MONITOR_SERVICE_RESULT:-$(show Result)}
status=${MONITOR_EXIT_STATUS:-$(show ExecMainStatus)}
invocation=${MONITOR_INVOCATION_ID:-$(show InvocationID)}

if [ -n "$invocation" ]; then
  journal=(journalctl -q -o cat --no-pager "_SYSTEMD_INVOCATION_ID=$invocation")
else
  journal=(journalctl -q -o cat --no-pager -u "$unit" -n 500)
fi
lines=$("${journal[@]}" | python3 -I "$OPS_DIR/ops_message.py" log-lines "$log_lines") ||
  lines="(couldn't read the journal)"

text=":rotating_light: *inbox-jev: $unit failed* on \`$(hostname)\` at $(date '+%F %T %Z')
Result: $result, exit status ${status:-?}
Last app log lines (level, message, account, run_id, error type; never email content):
\`\`\`
${lines:-(no app log lines in this run)}
\`\`\`
Full logs: \`journalctl -u $unit -n 200\`"

slack_post "$text"
if [ -n "$(env_value ALERT_SLACK_CHANNEL)" ]; then
  touch "$marker"
fi
