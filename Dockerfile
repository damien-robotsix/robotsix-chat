# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage: install the locked dependency set + the project into the
# system interpreter (/usr/local), exactly what the runtime stage copies.
# Standard robotsix Dockerfile pattern — see robotsix-standards, docker page.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Bring in the uv static binary (pinned to a released version for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# uv needs git to fetch the git-sourced dependencies declared under
# [tool.uv.sources] (robotsix-config, robotsix-llmio).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git="1:2.*" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install into the system interpreter (/usr/local) — NOT `uv sync`, which
# builds a project venv the runtime COPY would miss. Extras: claude-sdk for
# the LLM transport, tracing for Langfuse observability, memory for cognee.
# --no-hashes: the git-sourced first-party deps cannot carry hashes.
RUN uv export --frozen --no-emit-project --no-hashes \
        --extra claude-sdk --extra tracing --extra memory --extra render-url \
        -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage: copy the installed site-packages and console script from the
# builder — no uv, no git, no compilers. Node.js + the claude CLI are the one
# genuine runtime system dependency (claude-sdk transport spawns the CLI).
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/robotsix-chat /usr/local/bin/robotsix-chat
COPY --from=builder /usr/local/bin/playwright /usr/local/bin/playwright

# Install Node.js (LTS) and the claude CLI — required at runtime: the
# claude-sdk subscription transport spawns the `claude` CLI as a subprocess.
# Build-only packages and caches are pruned in the same layer.
RUN apt-get update \
    && apt-get install --only-upgrade -y --no-install-recommends liblzma5="5.8.*" \
    && apt-get install -y --no-install-recommends curl="8.*" gnupg="2.*" \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs="22.*" \
    && npm install -g @anthropic-ai/claude-code@2.1.199 \
    && claude --version \
    && apt-get purge -y --auto-remove curl gnupg \
    && apt-get clean \
    # npm/corepack are build-time-only (used just above to install the claude
    # CLI); dropping them from the runtime image removes their bundled
    # vulnerable deps (picomatch, sigstore flagged by the CI Trivy gate).
    && rm -rf /var/lib/apt/lists/* /root/.npm \
        /usr/lib/node_modules/npm /usr/lib/node_modules/corepack \
        /usr/bin/npm /usr/bin/npx /usr/bin/corepack

# Install Playwright's Chromium browser with its system dependencies.
# playwright is already in site-packages (copied from the builder); this
# step downloads the Chromium binary and the shared libraries it needs.
# Store browsers in a fixed path so the `app` user can find them at
# runtime — the default cache (~/.cache/ms-playwright) resolves to
# /root/.cache at build time (we run as root), invisible to `app`.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN mkdir -p /opt/playwright-browsers \
    && ( for attempt in $(seq 1 3); do \
           playwright install --with-deps chromium && exit 0; \
           echo "Attempt ${attempt} failed, retrying in 10s..." >&2; \
           sleep 10; \
         done; \
         exit 1 ) \
    && playwright --version \
    && chmod -R a+rX /opt/playwright-browsers

# Standardized robotsix container layout (see robotsix-standards, docker
# page): non-root user `app`, uid/gid 1000, home /home/app. Central-deploy
# sets the container user to the deployment uid at container-create time;
# $HOME is read-only at runtime — all writes go to the mounted volumes
# (/home/app/config, /data, /home/app/.claude). Build args allow other
# hosts to override for local builds.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} app \
    && useradd --create-home --uid ${APP_UID} --gid ${APP_GID} app
WORKDIR /home/app
USER app

# Cache the HuggingFace tokenizer (bge-m3) on the persistent /data mount so
# the cognee `memory` extra doesn't re-download it on every redeploy.
ENV HF_HOME=/data/huggingface
EXPOSE 8080

# Probe the in-container /health route using only the Python stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

ENTRYPOINT ["robotsix-chat"]
