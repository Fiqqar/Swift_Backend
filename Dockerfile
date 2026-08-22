# syntax=docker/dockerfile:1

FROM python:3.12.9-slim AS builder

WORKDIR /app

# Gunakan mirror resmi bawaan Debian + APT Cache Mount
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    pkg-config \
    libpq-dev \
    libgeos-dev

# Rust/Cargo Setup
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal

# Cache Wheel PIP
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install maturin && \
    pip wheel --prefer-binary -r requirements.txt -w /app/wheels

# Build Maturin/Rust dengan Cargo Cache
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    maturin build --release -o /app/wheels

# --- STAGE RUNNER ---
FROM python:3.12.9-slim AS runner

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# APT Cache Mount
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libgeos-c1v5 \
    libexpat1 \
    libgdal32 \
    libproj25 \
    curl

COPY --from=builder /app/wheels /wheels
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-index --find-links=/wheels -r requirements.txt \
    && pip install /wheels/*.whl \
    && rm -rf /wheels

COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Tempatkan penyalinan kode aplikasi di paling akhir
COPY . .

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --proxy-headers --port ${PORT:-8000}"]