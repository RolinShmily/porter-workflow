#!/usr/bin/env bash
# Pre-flight probe: is this link usable, and what will it give us?
#
# Cheaper than `porter.sh run` by design -- it fetches metadata and downloads no
# media, so it is safe to call before deciding whether to start a job.
#
#   inspect.sh "https://youtu.be/..."
#   inspect.sh "https://youtu.be/..." --json
#
# Exit code 1 means the link is unusable (404, private, unsupported host). That is
# a fact about the link, not a failure of the tool.
set -euo pipefail
exec uvx --from "porter-workflow[all]" porter inspect "$@"
