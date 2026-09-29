#!/usr/bin/env bash
# The designer's development dependencies are not used by any Lambda. SAM's
# CodeUri: . would otherwise copy ~140 MB of node_modules into every function.
set -euo pipefail
cd "$(dirname "$0")/.."

stash=.aws-sam/designer-node-modules
if [ -e "$stash" ]; then
  echo "Refusing to overwrite $stash; restore it to designer/node_modules first" >&2
  exit 1
fi
restore() {
  if [ -d "$stash" ]; then mv "$stash" designer/node_modules; fi
}
trap restore EXIT
if [ -d designer/node_modules ]; then
  mv designer/node_modules "$stash"
fi

# CopySource is incremental even when a directory disappeared from the input;
# clear only the generated build so stale dependencies cannot survive there.
rm -rf .aws-sam/build
sam build --config-env sandbox --no-cached
