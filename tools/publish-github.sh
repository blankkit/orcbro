#!/bin/sh
# Publish the current `main` to GitHub as one new commit.
#
# The public history on GitHub starts at Orcbro 1.0; the full history lives in the homelab GitLab (remote `origin`).
# Each release adds one snapshot commit on top of `github/main` with the tree of local `main`, optionally tagged
# (tags `v*` build the container image on GitHub).
#
#   tools/publish-github.sh "Orcbro 1.0.1" v1.0.1
set -eu

message=${1:?'usage: tools/publish-github.sh "Orcbro 1.0.1" [v1.0.1]'}
tag=${2:-}
git fetch --quiet github main
tree=$(git rev-parse 'main^{tree}')
parent=$(git rev-parse github/main)
if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
  echo "GitHub already has this state."
else
  commit=$(GIT_AUTHOR_NAME=blankkit GIT_AUTHOR_EMAIL=102239494+blankkit@users.noreply.github.com \
    GIT_COMMITTER_NAME=blankkit GIT_COMMITTER_EMAIL=102239494+blankkit@users.noreply.github.com \
    git commit-tree "$tree" -p "$parent" -m "$message")
  git push github "${commit}:refs/heads/main"
  parent=$commit
fi
if [ -n "$tag" ]; then
  # Lightweight tag on the snapshot; local tags with the same name belong to the full history.
  git push github "${parent}:refs/tags/${tag}"
fi
