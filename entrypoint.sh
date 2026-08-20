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

if [ "${SEED_ON_STARTUP:-}" = "true" ]; then
    if python scripts/check_seed_needed.py; then
        echo "[entrypoint] Data kosong, menyisipkan data dummy ..."
        python scripts/seed_kurir.py
        python scripts/seed_hub.py
        python scripts/seed_paket.py
        python scripts/seed_batch.py
        python scripts/seed_shipment.py
        echo "[entrypoint] Auto-seed selesai."
    else
        echo "[entrypoint] Data sudah ada, auto-seed dilewati."
    fi
fi

if [ -n "$TILES_DIR" ]; then
    exec env TILES_DIR="$TILES_DIR" "$@"
else
    exec "$@"
fi