#!/usr/bin/env bash
# Deploy a commit of this repo here, with the image CI built for it.
#
# Usage, on the server, from the deployment checkout (e.g. /opt/inbox-jev):
#   scripts/deploy.sh                 # origin/main: pull its image, restart the timer
#   scripts/deploy.sh --hold          # same, but leave the timer stopped: run the PR's one-off
#                                     # steps (reprocess, senders backfill, rules …), then
#                                     # sudo systemctl start inbox-jev-triage.timer
#   scripts/deploy.sh --build         # build the image here instead of pulling it (no registry)
#   scripts/deploy.sh <ref>           # a commit, tag or branch (origin/<branch>): the previous
#                                     # sha rolls back
#
# Steps: stop the triage timer and wait for a running pass; back up the SQLite DB; fetch and
# check out the ref, detached (the checkout only ever holds what is deployed); pull (or build)
# the image for that commit and record its tag in .env as IMAGE_TAG, which the timers'
# `docker compose run` reads; check that the image reports the checkout's commit
# (`inbox-jev --version`); start the timer.
#
# If anything fails after the timer was stopped, the script says so and leaves it stopped: fix
# the problem (restoring .env from the .env.bak-<stamp> it names if the image was already
# switched), then start the timer by hand. Run it as the user that owns the checkout.
set -euo pipefail

cd "$(dirname "$0")/.."
build=0
hold=0
ref=origin/main
for arg in "$@"; do
  case $arg in
    --build) build=1 ;;
    --hold) hold=1 ;;
    -*) echo "usage: $0 [--build] [--hold] [<ref>]" >&2; exit 2 ;;
    *) ref=$arg ;;
  esac
done
[ -f .env ] || { echo ".env not found: run from the deployment checkout" >&2; exit 2; }
stamp=$(date +%Y%m%d%H%M)
env_switched=0

on_exit() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "!! deploy failed (exit $status): the triage timer is STOPPED" >&2
    if [ "$env_switched" = 1 ]; then
      echo "!! .env already points at the new image; the previous .env is .env.bak-$stamp" >&2
    fi
    echo "!! fix the problem, then: sudo systemctl start inbox-jev-triage.timer" >&2
  fi
}

echo "== stopping the triage timer and waiting for a running pass"
sudo systemctl stop inbox-jev-triage.timer
trap on_exit EXIT
# The oneshot service is "activating" while a pass runs, so `is-active` wouldn't wait.
while [ "$(systemctl show -p ActiveState --value inbox-jev-triage.service)" = activating ]; do
  sleep 2
done

echo "== backing up the database"
# SQLite only (the default DATABASE_URL). data/ belongs to APP_UID with mode 700: needs sudo.
# The DB runs in WAL mode: sqlite3's .backup copies a consistent DB, WAL included.
if sudo test -f data/inbox-jev.db; then
  sudo sqlite3 data/inbox-jev.db ".backup 'data/inbox-jev.db.bak-$stamp'"
else
  echo "   no data/inbox-jev.db (hosted DB?): nothing to back up"
fi

echo "== checking out $ref"
git fetch -q origin
# A branch name means the remote's branch: the local one never moves (the checkout is detached).
if git rev-parse --verify -q "refs/remotes/origin/$ref" >/dev/null; then
  ref=origin/$ref
fi
git checkout -q --detach "$ref"
sha=$(git rev-parse HEAD)
tag="sha-${sha:0:7}"
echo "   $(git log -1 --format='%h %ci %s')"

echo "== image $tag"
if [ "$build" = 1 ]; then
  GIT_SHA=$sha IMAGE_TAG=$tag docker compose build app
elif ! IMAGE_TAG=$tag docker compose pull app; then
  echo "pull failed: 'docker login ghcr.io' with a token that has read:packages, or use --build" >&2
  exit 1
fi
# Record the tag: compose reads .env for ${IMAGE_TAG}, so the timers run this image from now on.
cp -p .env ".env.bak-$stamp"
if grep -q '^IMAGE_TAG=' .env; then
  sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$tag/" .env
else
  printf '\n# Set by scripts/deploy.sh: the image the timers run\nIMAGE_TAG=%s\n' "$tag" >> .env
fi
env_switched=1

echo "== smoke test"
reported=$(docker compose run --rm --no-deps -T app --version)
echo "   $reported"
case $reported in
  *"($sha)"*) ;;
  *) echo "the image doesn't report the checkout's commit $sha" >&2; exit 1 ;;
esac

if [ "$hold" = 1 ]; then
  echo "== timer left stopped. Run the PR's one-off steps, then: sudo systemctl start inbox-jev-triage.timer"
else
  sudo systemctl start inbox-jev-triage.timer
  echo "== done: $tag deployed, timer started"
fi
