#!/bin/sh
set -e

# 1. Penanganan Tile Lokal
TILES_SRC="${TILES_SRC:-/app/data/tiles}"
TILES_LOCAL="${TILES_LOCAL:-/tmp/tiles}"

if [ -f "${TILES_SRC}/manifest.json" ]; then
    echo "[entrypoint] Meng-copy tile lokal ke ${TILES_LOCAL} ..."
    mkdir -p "${TILES_LOCAL}"
    cp -a "${TILES_SRC}/." "${TILES_LOCAL}/"
    echo "[entrypoint] Tile lokal siap di ${TILES_LOCAL}."
    
    export TILES_DIR="${TILES_LOCAL}"
fi

PBF_DIR="/app/data/pbf"
PBF_FILE="${PBF_DIR}/map.osm.pbf"

mkdir -p "${PBF_DIR}"

if [ ! -f "${PBF_FILE}" ]; then
    echo "[entrypoint] File PBF tidak ditemukan. Mengunduh dari Storage..."
    
    if [ -n "$PBF_URL" ]; then
        curl -L -o "${PBF_FILE}" "${PBF_URL}" || echo "[entrypoint] WARNING: Gagal mengunduh file PBF!"
    else
        echo "[entrypoint] ERROR: Environment variable PBF_URL belum diset di Railway!"
    fi

    if [ -f "${PBF_FILE}" ]; then
        echo "[entrypoint] Download file PBF selesai: ${PBF_FILE}"
    fi
else
    echo "[entrypoint] File PBF sudah tersedia di ${PBF_FILE}."
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

# 4. Jalankan Aplikasi Utama
if [ -n "$TILES_DIR" ]; then
    exec env TILES_DIR="$TILES_DIR" "$@"
else
    exec "$@"
fi