#!/usr/bin/env bash
# Drift detector for git-managed deploy targets.
#
# Flags when a deployed working tree has diverged from its tracked upstream:
#   - dirty  : uncommitted tracked changes (a server hand-edit never pushed)
#   - ahead  : local commits not on origin   (the prism-surface failure mode)
#   - behind : deployed code older than the branch (a deploy that didn't land)
#
# Policy is "edit allowed, but must push": this NAGS (logs + state file),
# it never auto-reverts. Runs as pmsvc so it shares .git ownership with the
# runner and deploys (root-owned .git would break the runner's fetch).
#
# Add services to TARGETS as they onboard. The script lives in the repo and
# is auto-updated to /srv by the deploy pipeline, so editing it here is enough.
set -uo pipefail

TARGETS=(
  "train-tracker|/srv/train-tracker|master"
)

STATE=/var/lib/ci-drift/status
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
tmp="$(mktemp)"
drift=0

notify() { # $1=name $2=summary — pluggable; wire to dispatch/overwatch later
  logger -t ci-drift -p user.warning "$1: $2"
}

for t in "${TARGETS[@]}"; do
  IFS='|' read -r name path branch <<<"$t"
  if [ ! -d "$path/.git" ]; then
    echo "$name MISSING-REPO $path"; echo "$name MISSING-REPO" >>"$tmp"; drift=1; continue
  fi
  git -C "$path" fetch -q origin "$branch" 2>/dev/null || true
  dirty=$(git -C "$path" status --porcelain --untracked-files=no | wc -l | tr -d ' ')
  ahead=$(git -C "$path" rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo 0)
  behind=$(git -C "$path" rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)
  if [ "$dirty" -gt 0 ] || [ "$ahead" -gt 0 ] || [ "$behind" -gt 0 ]; then
    drift=1
    msg="DRIFT dirty=$dirty ahead=$ahead behind=$behind — reconcile (commit+push) needed"
    echo "$name $msg"
    echo "$name $msg" >>"$tmp"
    notify "$name" "$msg"
  else
    echo "$name OK"
    echo "$name OK" >>"$tmp"
  fi
done

mv "$tmp" "$STATE" 2>/dev/null || cp "$tmp" "$STATE"
# Exit 0 even on drift: a found-drift is a normal, expected outcome surfaced via
# the warning-level journal nag + status file (not a script malfunction). Reserve
# non-zero for actual errors so `systemctl --failed` stays meaningful.
exit 0
