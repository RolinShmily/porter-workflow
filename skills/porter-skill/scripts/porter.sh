#!/usr/bin/env bash
# Run porter without installing anything.
#
# `uvx` resolves the distribution in an ephemeral environment, so the agent does
# not need to create a virtualenv, activate it, or know where porter lives.
# The base distribution includes yt-dlp. [llm] adds keyed Whisper/LLM support;
# local ASR, MCP and WebUI remain opt-in rather than slowing every inspect.
#
# Every argument is forwarded verbatim, so this is the CLI:
#   porter.sh run "https://youtu.be/..." --burn zh-only
#   porter.sh inspect "https://youtu.be/..." --json
#   porter.sh jobs status <job_id>
set -euo pipefail

# Opt in by setting this to an absolute directory inside the host's writable
# workspace. Never infer a workspace from the installed skill path: hosts put
# skills under different roots, and a guessed parent can escape the sandbox.
if [[ -n "${PORTER_SKILL_STATE_DIR:-}" ]]; then
    state="$PORTER_SKILL_STATE_DIR"
    if [[ "$state" != /* ]]; then
        echo "porter-skill: PORTER_SKILL_STATE_DIR must be absolute: $state" >&2
        exit 2
    fi
    export UV_CACHE_DIR="$state/uv-cache"
    export UV_TOOL_DIR="$state/uv-tools"
    export UV_TOOL_BIN_DIR="$state/uv-tools/bin"
    export UV_PYTHON_INSTALL_DIR="$state/uv-python"
    export UV_PYTHON_BIN_DIR="$state/uv-python/bin"
    export XDG_CACHE_HOME="$state/cache"
    export PORTER_CACHE_DIR="$state/porter-cache"
    # yt-dlp respects XDG_CACHE_HOME; porter uses PORTER_CACHE_DIR even on
    # Windows, where platformdirs ignores XDG_CACHE_HOME.
fi

check_dir() {
    local dir="$1" file
    if ! mkdir -p "$dir" 2>/dev/null; then
        echo "porter-skill: cannot create $dir; select a writable workspace with PORTER_SKILL_STATE_DIR" >&2
        return 1
    fi
    file="$dir/.porter-skill-write-$$"
    if ! (umask 077; : > "$file") 2>/dev/null; then
        echo "porter-skill: cannot write $dir; select a writable workspace with PORTER_SKILL_STATE_DIR" >&2
        return 1
    fi
    rm -f -- "$file"
}

host_check() {
    local name dir
    if [[ -z "${PORTER_SKILL_STATE_DIR:-}" && "${1:-}" == --host-check ]]; then
        echo 'porter-skill: no workspace state directory configured; host defaults cannot be certified.' >&2
        echo 'porter-skill: in a restricted host set PORTER_SKILL_STATE_DIR to an absolute writable workspace path.' >&2
    fi
    for name in UV_CACHE_DIR UV_TOOL_DIR UV_TOOL_BIN_DIR UV_PYTHON_INSTALL_DIR UV_PYTHON_BIN_DIR XDG_CACHE_HOME PORTER_CACHE_DIR; do
        dir="${!name:-}"
        if [[ -n "$dir" ]]; then
            check_dir "$dir" || return 1
        fi
    done
    echo 'porter-skill: configured writable directories OK (external commands and network not tested).' >&2
}

if [[ "${1:-}" == --host-check ]]; then
    host_check
    exit
fi

# Check paths before invoking uvx; otherwise a sandbox denial inside uv appears
# as a Python dependency failure. In the default mode, only configured paths are
# checked; unknown host defaults cannot be certified without running uvx.
host_check || exit 1
spec="${PORTER_SKILL_SPEC:-porter-workflow[llm]}"
exec uvx --from "$spec" porter "$@"
