#!/usr/bin/env bash
# Install (or reinstall) the ha_rbac integration into the local HA config, then
# restart Home Assistant and tail its log. Run from inside the rpi-test/
# directory (local Podman; Docker also works with ENGINE=docker).
#
# By default it installs the upstream integration from main. Override with env
# vars to test a fork or a branch:
#   REPO=https://github.com/you/ha-rbac BRANCH=my-branch ./install-integration.sh
#
# Usage:
#   ./install-integration.sh            # clone REPO@BRANCH and install
#   ./install-integration.sh --local /path/to/ha-rbac   # install from a local checkout

set -euo pipefail

REPO="${REPO:-https://github.com/FezVrasta/ha-rbac}"
BRANCH="${BRANCH:-main}"

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_engine.sh
source "$HERE/_engine.sh"
CONFIG_DIR="$HERE/config"
CC_DIR="$CONFIG_DIR/custom_components"
DEST="$CC_DIR/ha_rbac"

mkdir -p "$CC_DIR"

SRC=""
CLEANUP=""

if [[ "${1:-}" == "--local" ]]; then
  # Path is optional: default to the repo this harness lives in, so a bare
  # --local installs the current checkout including uncommitted edits.
  LOCAL_PATH="${2:-$HERE/..}"
  SRC="$LOCAL_PATH/custom_components/ha_rbac"
  if [[ ! -d "$SRC" ]]; then
    echo "No custom_components/ha_rbac under $LOCAL_PATH" >&2
    exit 1
  fi
  echo "Installing from local checkout: $LOCAL_PATH"
else
  TMP="$(mktemp -d)"
  CLEANUP="$TMP"
  echo "Cloning $REPO ($BRANCH) ..."
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$TMP"
  SRC="$TMP/custom_components/ha_rbac"
fi

echo "Installing integration into $DEST ..."
rm -rf "$DEST"
cp -r "$SRC" "$DEST"

# Report the version being installed, so you can tell it apart from a stale one.
if command -v python3 >/dev/null 2>&1; then
  VERSION="$(python3 -c "import json,sys; print(json.load(open('$DEST/manifest.json'))['version'])" 2>/dev/null || echo '?')"
  echo "Installed ha_rbac version: $VERSION"
fi

[[ -n "$CLEANUP" ]] && rm -rf "$CLEANUP"

echo "Restarting Home Assistant ($ENGINE) ..."
COMPOSE restart homeassistant

# setup.sh sets NO_TAIL=1 so the orchestration does not block on the log tail.
if [[ "${NO_TAIL:-}" == "1" ]]; then
  echo "Restarted. (NO_TAIL set; not tailing.)"
  exit 0
fi

echo "Tailing logs (Ctrl-C to stop) ..."
COMPOSE logs -f homeassistant
