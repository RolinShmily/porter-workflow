#!/usr/bin/env bash
# Run porter without installing anything.
#
# `uvx` resolves the distribution in an ephemeral environment, so the agent does
# not need to create a virtualenv, activate it, or know where porter lives. The
# [all] extra pulls in the yt-dlp and MCP extras; without it a run fails at the
# download phase with an import error that looks like a bug in the video.
#
# Every argument is forwarded verbatim, so this is the CLI:
#   porter.sh run "https://youtu.be/..." --burn zh-only
#   porter.sh inspect "https://youtu.be/..." --json
#   porter.sh jobs status <job_id>
set -euo pipefail
exec uvx --from "porter-workflow[all]" porter "$@"
