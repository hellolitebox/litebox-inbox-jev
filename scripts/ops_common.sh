# shellcheck shell=bash
# Helpers shared by the host-side ops scripts (alert.sh, watchdog.sh, backup.sh, deploy.sh).
# Sourced, never run. They run on the host from systemd with bash, curl, python3 (standard
# library only, through ops_message.py), sqlite3 and journalctl: never in the app image, so an
# alert still goes out when the image is broken or can't be pulled.
#
# Settings come from the environment, else from the deployment's .env (see .env.example,
# "Host-side ops scripts"). Never `set -x` in a script that sources this: the Slack token would
# be traced.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OPS_ROOT=$(dirname "$OPS_DIR")

# env_value NAME [DEFAULT]: NAME from the environment when set there (even empty), else the last
# `NAME=` line of .env, else DEFAULT. .env is read, never sourced (values may contain spaces):
# surrounding quotes are dropped, and an unquoted value's ` # comment`, as docker compose does.
env_value() {
  local name=$1 default=${2-} value
  if [ -n "${!name+x}" ]; then
    printf '%s' "${!name}"
    return
  fi
  value=$(grep -E "^${name}=" "$OPS_ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r') || true
  case $value in
    \"*) value=${value#\"}; value=${value%%\"*} ;;
    \'*) value=${value#\'}; value=${value%%\'*} ;;
    *)
      value=${value%%[[:space:]]#*}
      value=${value%"${value##*[![:space:]]}"}
      ;;
  esac
  printf '%s' "${value:-$default}"
}

# require_count NAME VALUE MIN: fail unless VALUE is a whole number >= MIN.
require_count() {
  case $2 in
    '' | *[!0-9]*) echo "$1 must be a whole number, got '$2'" >&2; return 2 ;;
  esac
  if [ "$2" -lt "$3" ]; then
    echo "$1 must be at least $3, got $2" >&2
    return 2
  fi
}

# state_dir: where the alert and watchdog keep their dedupe files (systemd's StateDirectory=).
state_dir() {
  local dir=${STATE_DIRECTORY:-/var/lib/inbox-jev}
  mkdir -p "$dir"
  printf '%s' "$dir"
}

# slack_post TEXT: post TEXT (Slack mrkdwn; &, < and > are escaped) to ALERT_SLACK_CHANNEL with
# SLACK_BOT_TOKEN. Without ALERT_SLACK_CHANNEL alerts are off: say so and succeed. The token goes
# to curl on stdin (a config file), never on its command line or in the output.
slack_post() {
  local channel token payload response error
  channel=$(env_value ALERT_SLACK_CHANNEL)
  if [ -z "$channel" ]; then
    echo "ALERT_SLACK_CHANNEL is not set: alerts are off, not sent" >&2
    return 0
  fi
  token=$(env_value SLACK_BOT_TOKEN)
  if [ -z "$token" ]; then
    echo "SLACK_BOT_TOKEN is not set: can't send the alert" >&2
    return 1
  fi
  payload=$(printf '%s' "$1" | python3 -I "$OPS_DIR/ops_message.py" payload "$channel")
  response=$(printf 'header = "Authorization: Bearer %s"\n' "$token" |
    curl -sS --max-time 20 --retry 2 -K - \
      -H 'Content-Type: application/json; charset=utf-8' \
      --data-binary "$payload" https://slack.com/api/chat.postMessage) || {
    echo "couldn't reach Slack: alert not sent" >&2
    return 1
  }
  case $response in
    *'"ok":true'*) echo "alert sent to $channel" ;;
    *)
      error=$(printf '%s' "$response" | sed -n 's/.*"error":"\([^"]*\)".*/\1/p')
      echo "Slack refused the alert: ${error:-unknown error}" >&2
      return 1
      ;;
  esac
}

# prune_oldest KEEP DIR PREFIX [CMD…]: delete all but the KEEP newest (by mtime) files in DIR
# whose name starts with PREFIX. CMD prefixes `ls` and `rm` (e.g. sudo, for a mode-700 data/).
prune_oldest() {
  local keep=$1 dir=$2 prefix=$3 name
  shift 3
  "$@" ls -1At -- "$dir" | awk -v p="$prefix" 'index($0, p) == 1' | tail -n +"$((keep + 1))" |
    while IFS= read -r name; do
      echo "   removing old backup $dir/$name"
      "$@" rm -f -- "$dir/$name"
    done
}
