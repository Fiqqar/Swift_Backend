#!/bin/sh
set -e

TILES_SRC="${TILES_SRC:-/app/data/tiles}"
TILES_LOCAL="${TILES_LOCAL:-/tmp/tiles}"

if [ -f "${TILES_SRC}/manifest.json" ]; then
    echo "[entrypoint] Meng-copy tile lokal ke ${TILES_LOCAL} ..."
    mkdir -p "${TILES_LOCAL}"
    cp -a "${TILES_SRC}/." "${TILES_LOCAL}/"
    echo "[entrypoint] Tile lokal siap di ${TILES_LOCAL}."
    export TILES_DIR="${TILES_LOCAL}"
fi

exec "$@"
