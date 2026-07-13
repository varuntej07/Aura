# =============================================================================
# VOICE WORKER image (LiveKit Cloud agent).
# Named `Dockerfile` on purpose: LiveKit Cloud's managed builder only reads a
# file named exactly `Dockerfile` in the deploy dir — there is no --dockerfile
# flag or [build] override. The FastAPI backend image is `Dockerfile.api`, built
# explicitly with `-f Dockerfile.api` (see deploy.sh and the dark-deploy command).
# =============================================================================
# ── Install dependencies ────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install third-party dependencies keyed only on pyproject.toml. A stub package lets
# `pip install .` resolve without the real source, so editing code does NOT invalidate
# this layer (nor the ~332MB download-files layer below it that depends on it). Deps
# reinstall only when pyproject.toml or constraints.txt changes. The real src/ is
# copied into the runtime stage from the build context below, not from this builder.
COPY pyproject.toml constraints.txt ./
RUN mkdir -p src && touch src/__init__.py

# -c constraints.txt: this is a clean container, so pip must resolve the WHOLE
# graph from scratch every build. Nearly every dep in pyproject.toml is a bare
# `>=X` floor with no upper bound, and livekit-agents pulls in tightly-pinned
# transitives (click, aiohttp, opentelemetry-*) that fight those loose floors,
# so pip's backtracking resolver can hit "resolution-too-deep" trying every
# historical version before it (eventually) finds a compatible set. The
# constraints file is that already-verified compatible set (see its own header
# for how to regenerate it) — it doesn't add requirements, it just removes the
# search.
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -c constraints.txt .

# ── Lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

RUN groupadd -r juno && useradd -r -g juno -m juno

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Download the turn-detector ONNX model and Silero VAD weights into the image
# so the worker subprocess doesn't try to fetch them at runtime.
# Placed BEFORE `COPY src/` so editing source code doesn't invalidate this ~332MB layer 
# it depends only on the installed livekit.agents package, not on src.
ENV HF_HOME=/app/.cache/huggingface
RUN mkdir -p $HF_HOME && python -m livekit.agents download-files

COPY src/ ./src/

RUN chown -R juno:juno /app

USER juno

# Cloud Run injects PORT. WorkerOptions reads it via os.environ.get("PORT").
# The worker binds its health/metrics HTTP server to this port so Cloud Run health checks pass. 
# The actual LiveKit WebSocket is outbound so no inbound port needed for the voice pipeline itself.
EXPOSE 8080

CMD ["python", "-m", "src.agent.voice_agent", "start"]