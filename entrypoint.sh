#!/bin/sh
set -e

# 1Penanganan Tile Lokal
TILES_SRC="${TILES_SRC:-/app/data/tiles}"
TILES_LOCAL="${TILES_LOCAL:-/tmp/tiles}"

if [ -f "${TILES_SRC}/manifest.json" ]; then
    echo "[entrypoint] Meng-copy tile lokal ke ${TILES_LOCAL} ..."
    mkdir -p "${TILES_LOCAL}"
    cp -a "${TILES_SRC}/." "${TILES_LOCAL}/"
    echo "[entrypoint] Tile lokal siap di ${TILES_LOCAL}."
    
    export TILES_DIR="${TILES_LOCAL}"
fi

# Penanganan File PBF
PBF_DIR="/app/data/pbf"
PBF_FILE="${PBF_DIR}/java-260805.osm.pbf"

mkdir -p "${PBF_DIR}"

if [ ! -f "${PBF_FILE}" ]; then
    echo "[entrypoint] File PBF tidak ditemukan. Mengunduh dari Storage..."
    
    if [ -n "$PBF_URL" ]; then
        if [ -n "$GH_TOKEN" ]; then
            echo "[entrypoint] Mencari Asset ID dari GitHub Release..."
            
            ASSET_ID=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
              "https://api.github.com/repos/mmm-chd/pathfinding_test/releases/tags/v1.0.0-jawa" \
              | grep -B 2 '"name": "java-260805.osm.pbf"' | grep '"id":' | head -n 1 | awk '{print $2}' | tr -d ',')

            if [ -n "$ASSET_ID" ]; then
                echo "[entrypoint] Mengunduh Asset ID: ${ASSET_ID}..."
                curl -L -H "Authorization: Bearer $GH_TOKEN" \
                     -H "Accept: application/octet-stream" \
                     -o "${PBF_FILE}" \
                     "https://api.github.com/repos/mmm-chd/pathfinding_test/releases/assets/${ASSET_ID}" || echo "[entrypoint] WARNING: Gagal mengunduh file PBF!"
            else
                echo "[entrypoint] ERROR: Asset ID tidak ditemukan di Release GitHub!"
            fi
        else
            echo "[entrypoint] Mengunduh dari Public URL..."
            curl -L -o "${PBF_FILE}" "${PBF_URL}" || echo "[entrypoint] WARNING: Gagal mengunduh file PBF!"
        fi
    else
        echo "[entrypoint] ERROR: Environment variable PBF_URL belum diset di Railway!"
    fi

    if [ -f "${PBF_FILE}" ]; then
        echo "[entrypoint] Download file PBF selesai: ${PBF_FILE}"
    fi
else
    echo "[entrypoint] File PBF sudah tersedia di ${PBF_FILE}."
fi

# Auto-Seeding Database
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