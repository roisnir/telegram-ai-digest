#!/usr/bin/env bash
# Build the digest Docker image, refusing to build from a dirty working tree
# and baking the git branch + commit into the image for traceability.
#
# Usage:
#   ./build.sh            # tag = short commit, plus :latest
#   ./build.sh v0.0.4     # tag = v0.0.4, plus :latest
set -euo pipefail

IMAGE_NAME=telegram-ai-digest

# --- Refuse to build from a dirty tree -------------------------------------
# `git status --porcelain` lists modified, staged, and untracked files but
# ignores .gitignored ones (.env, *.dockerimage, session.session, etc.), so a
# clean result means the build context matches exactly what is committed.
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: git working tree is not clean — commit or stash first." >&2
  git status --short >&2
  exit 1
fi

GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_COMMIT=$(git rev-parse --short HEAD)
TAG=${1:-$GIT_COMMIT}

docker build \
  --build-arg IMAGE_TAG="$TAG" \
  --build-arg GIT_BRANCH="$GIT_BRANCH" \
  --build-arg GIT_COMMIT="$GIT_COMMIT" \
  -t "$IMAGE_NAME:$TAG" -t "$IMAGE_NAME:latest" .

echo "Built $IMAGE_NAME:$TAG  (branch=$GIT_BRANCH commit=$GIT_COMMIT)"
