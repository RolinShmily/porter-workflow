#!/usr/bin/env bash
# Fallback for environments where `uvx` is unavailable (offline hosts, the
# packaged EXE, or a user who wants porter in a project virtualenv).
#
# Prefer scripts/porter.sh. This exists because `uvx` needs network access on
# first use, and a skill that cannot run at all on an offline machine is worse
# than one that needs a manual step.
#
# Usage, from a checkout of the repository:
#   skills/porter-skill/scripts/bootstrap.sh
#
# Then invoke porter from that virtualenv's bin/ directory.
set -euo pipefail

# Walk up to the repository root: this file is at skills/porter-skill/scripts/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

if [[ ! -f "$REPO_ROOT/pyproject.toml" ]]; then
    echo "bootstrap.sh must run from a porter-workflow checkout; looked in $REPO_ROOT" >&2
    exit 1
fi

cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
    echo "==> Creating .venv with uv"
    uv venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    uv pip install -e ".[all]"
else
    echo "==> Creating .venv with python3 -m venv"
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install -e ".[all]"
fi

echo "==> Installed. Check the host with:"
echo "    .venv/bin/porter doctor"
