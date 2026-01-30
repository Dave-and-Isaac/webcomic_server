#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/VERSION"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"

if [[ ! -f "$VERSION_FILE" ]]; then
  echo "VERSION file not found" >&2
  exit 1
fi

VERSION="$(tr -d ' \n' < "$VERSION_FILE")"
if [[ -z "$VERSION" ]]; then
  echo "VERSION is empty" >&2
  exit 1
fi

IMAGE_NAME="webcomic-reader"

# Build with version + latest tags
( cd "$ROOT_DIR" && docker build -t "$IMAGE_NAME:$VERSION" -t "$IMAGE_NAME:latest" . )

# Update docker-compose.yml image tag and APP_VERSION
if [[ -f "$COMPOSE_FILE" ]]; then
  tmp="${COMPOSE_FILE}.tmp"
  awk -v ver="$VERSION" '
    $1 ~ /^image:/ {print "    image: webcomic-reader:" ver; next}
    $1 ~ /^- APP_VERSION=/ {print "      - APP_VERSION=" ver; next}
    {print}
  ' "$COMPOSE_FILE" > "$tmp"
  mv "$tmp" "$COMPOSE_FILE"
fi

echo "Built $IMAGE_NAME:$VERSION and $IMAGE_NAME:latest"
