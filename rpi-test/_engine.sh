#!/usr/bin/env bash
# Shared container-engine detection, sourced by the other scripts.
#
# Prefers Podman. Sets:
#   $ENGINE   -> "podman" or "docker"       (for `exec`, etc.)
#   COMPOSE() -> a function that runs the right compose command
#
# Override with ENGINE=docker ./script.sh if you want to force Docker.

if [[ -z "${ENGINE:-}" ]]; then
  if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    echo "Neither podman nor docker found on PATH." >&2
    exit 1
  fi
fi

# Pick the compose front-end once. `podman compose` (v4+) and the standalone
# `podman-compose` are both supported; likewise `docker compose` v2.
if [[ "$ENGINE" == "podman" ]]; then
  if podman compose version >/dev/null 2>&1; then
    COMPOSE() { podman compose "$@"; }
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE() { podman-compose "$@"; }
  else
    echo "podman found, but no 'podman compose' or 'podman-compose'." >&2
    echo "Install one: 'pip install podman-compose' or Podman 4.x+." >&2
    exit 1
  fi
else
  COMPOSE() { docker compose "$@"; }
fi
