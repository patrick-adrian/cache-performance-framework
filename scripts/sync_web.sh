#!/usr/bin/env bash
# Refresh the static catalog's RTL mirror (and optional CSV → JSON later).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/web/rtl"
cp -f "$ROOT"/rtl/*.sv "$ROOT/web/rtl/"
echo "Synced $(ls "$ROOT"/rtl/*.sv | wc -l | tr -d ' ') RTL files → web/rtl/"
