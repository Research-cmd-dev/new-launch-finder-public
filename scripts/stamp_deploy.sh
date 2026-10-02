#!/usr/bin/env sh
# Stamp the tree right before `railway up` so /health can name what runs.
#
#   launchfinder/.deploy_stamp  UTC time of the stamp (busts the Docker COPY cache)
#   launchfinder/.git_sha       short SHA of the code commit being shipped
#
# The stamp commit itself only adds these two files, so `.git_sha` names its
# parent — the last commit that changed code. Usage, from a clean tip tree
# with IMAGE_REV already bumped:
#
#   sh scripts/stamp_deploy.sh && git commit -am "Touch deploy stamp for $(python3 -c 'from launchfinder.image_rev import IMAGE_REV; print(IMAGE_REV)')"
#   railway up ...   # both services, see docs/RUNBOOK.md
#
# Then `curl /health | jq .image_rev,.git_sha,.deploy_stamp` must echo the
# IMAGE_REV, this SHA and this stamp.
set -eu
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain -- launchfinder scripts Dockerfile tests | grep -v '^.. launchfinder/\.\(deploy_stamp\|git_sha\)$' || true)" ]; then
  echo "stamp_deploy: tree has uncommitted code changes; commit them first" >&2
  exit 1
fi
date -u +%Y%m%dT%H%M%SZ > launchfinder/.deploy_stamp
git rev-parse --short=12 HEAD > launchfinder/.git_sha
echo "deploy_stamp $(cat launchfinder/.deploy_stamp)  git_sha $(cat launchfinder/.git_sha)"
