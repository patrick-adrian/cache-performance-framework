#!/usr/bin/env bash
#
# One-shot toolchain install for the cache performance framework.
#
#   bash scripts/setup_wsl.sh [--with-optional]
#
# Idempotent: re-running it on a provisioned machine installs nothing and
# just re-reports the versions.

set -euo pipefail

REQUIRED_PKGS=(verilator build-essential python3-pip python3-venv)
OPTIONAL_PKGS=(iverilog gtkwave)
MIN_VERILATOR_MAJOR=5

with_optional=0
for arg in "$@"; do
    case "$arg" in
        --with-optional) with_optional=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This script provisions the Linux side of the toolchain." >&2
    echo "Run it inside WSL, not from Windows." >&2
    exit 1
fi

missing=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done

if (( with_optional )); then
    for pkg in "${OPTIONAL_PKGS[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
    done
fi

if (( ${#missing[@]} )); then
    echo "Installing: ${missing[*]}"
    # Prompts for a password unless sudo is configured passwordless.
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
else
    echo "All packages already present."
fi

echo
echo "Installed versions:"
verilator --version | sed 's/^/  /'
g++ --version | head -1 | sed 's/^/  /'
make --version | head -1 | sed 's/^/  /'
python3 --version | sed 's/^/  /'

# Verilator 4.x parses far less SystemVerilog than this project relies on, so a
# too-old install is a hard failure rather than a warning.
verilator_major="$(verilator --version | awk '{print $2}' | cut -d. -f1)"
if (( verilator_major < MIN_VERILATOR_MAJOR )); then
    echo
    echo "Verilator $verilator_major.x is too old; $MIN_VERILATOR_MAJOR.0 or newer is required." >&2
    exit 1
fi

# The distribution's python3 is marked externally managed, so the package and
# its dependencies go in a virtualenv. It sits beside the build cache rather
# than in the repo: same reason, /mnt/c is a slow place to keep many files.
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
venv="$HOME/.cache/cache-perf/venv"

if [[ ! -x "$venv/bin/python" ]]; then
    echo
    echo "Creating virtualenv at $venv"
    python3 -m venv "$venv"
fi

echo
echo "Installing cacheperf and its dependencies"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet --editable "$repo_root[dev]"

echo
echo "Next:"
echo "  source $venv/bin/activate"
echo "  python -m cacheperf.preflight --smoke"
