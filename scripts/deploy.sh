#!/usr/bin/env bash
# Auto-deploy for train-tracker.
# Runs on the self-hosted GitHub Actions runner (user: pmsvc) on big-server,
# invoked by .github/workflows/deploy.yml on push to master.
#
# Safety properties:
#  - Aborts if the deployed tree has uncommitted TRACKED changes, so an
#    un-pushed hand-edit on the server is never silently clobbered
#    ("edit allowed, but must push" policy). The drift-detector nags about it.
#  - Health-checks after restart and rolls back to the previous commit on failure.
#  - The workflow holds a concurrency lock so two deploys can never race.
set -euo pipefail

APP_DIR=/srv/train-tracker
SERVICE=train-tracker.service
HEALTH_URL=http://127.0.0.1:3974/
BRANCH=master

cd "$APP_DIR"

echo "::group::Preflight (drift check)"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ABORT: $APP_DIR has uncommitted tracked changes — a server hand-edit was never pushed."
  echo "Reconcile first (commit+push the edit, or discard it), then re-run this deploy."
  git status --short
  exit 1
fi
PREV=$(git rev-parse HEAD)
echo "current commit: $PREV"
echo "::endgroup::"

echo "::group::Fetch + reset to origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
TARGET=$(git rev-parse "origin/$BRANCH")
echo "target commit:  $TARGET"
if [ "$PREV" = "$TARGET" ]; then
  echo "already at target; redeploying anyway (restart + health check)."
fi
git reset --hard "origin/$BRANCH"
echo "::endgroup::"

echo "::group::Dependencies"
if [ -f requirements.txt ] && [ -x ./.venv/bin/pip ]; then
  ./.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "deps synced from requirements.txt"
else
  echo "no requirements.txt / venv pip — skipping"
fi
echo "::endgroup::"

echo "::group::Restart $SERVICE"
sudo /usr/bin/systemctl restart "$SERVICE"
echo "::endgroup::"

echo "::group::Health check ($HEALTH_URL)"
ok=0; code=000
for i in $(seq 1 15); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" || echo 000)
  if [ "$code" = "200" ]; then ok=1; echo "healthy (HTTP 200) after ${i}s"; break; fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  echo "UNHEALTHY after restart (last HTTP $code) — rolling back to $PREV"
  git reset --hard "$PREV"
  [ -f requirements.txt ] && [ -x ./.venv/bin/pip ] && ./.venv/bin/pip install --quiet -r requirements.txt || true
  sudo /usr/bin/systemctl restart "$SERVICE"
  echo "rolled back to $PREV."
  exit 1
fi
echo "::endgroup::"

echo "Deployed $TARGET successfully."
