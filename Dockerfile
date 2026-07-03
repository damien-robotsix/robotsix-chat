# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage: resolve and install locked dependencies + the project into a
# self-contained virtual environment that the runtime stage can simply COPY.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061 AS builder

# Bring in the uv static binary (pinned to a released version for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Create the self-contained virtual environment up front.
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Copy only what is needed to resolve and build the project.
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY README.md ./

# uv needs git to fetch the git-sourced dependencies declared under
# [tool.uv.sources] (robotsix-yaml-config, robotsix-llmio).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Export the locked dependency set (claude-sdk for the LLM transport + tracing
# for Langfuse observability), install it, then install the project itself
# without re-resolving dependencies.
RUN uv export --frozen --no-emit-project --no-hashes --extra claude-sdk --extra tracing --extra memory --extra broker > requirements.txt \
    && uv pip install --python /opt/venv/bin/python -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps .

# ---------------------------------------------------------------------------
# Runtime stage: minimal image with Node.js + the claude CLI and the prebuilt
# virtual environment, running as a non-root user.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061 AS runtime

# Install Node.js (LTS) and the claude CLI, then prune build-only packages and
# caches to keep the layer lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && claude --version \
    && apt-get purge -y --auto-remove curl gnupg \
    && apt-get clean \
    # npm/corepack are build-time-only (used just above to install the claude
    # CLI); dropping them from the runtime image removes their bundled
    # vulnerable deps (picomatch, sigstore flagged by the CI Trivy gate).
    && rm -rf /var/lib/apt/lists/* /root/.npm \
        /usr/lib/node_modules/npm /usr/lib/node_modules/corepack \
        /usr/bin/npm /usr/bin/npx /usr/bin/corepack

# Copy the prebuilt virtual environment (deps + project) from the builder stage.
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Standardized robotsix container layout (see robotsix-standards, docker
# page): non-root user `app`, uid/gid 1001, home /home/app. The UID matches
# the deploy host's operator so mounted mode-600 credentials (~/.claude for
# the level-3 claude-sdk transport; central-deploy binds it to
# /home/app/.claude) are readable. Build args allow other hosts to override.
ARG APP_UID=1001
ARG APP_GID=1001
RUN groupadd --gid ${APP_GID} app \
    && useradd --create-home --uid ${APP_UID} --gid ${APP_GID} app
WORKDIR /home/app
USER app

# Bind to all interfaces on 8080 inside the container.
ENV SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8080 \
    # Cache the HuggingFace tokenizer (bge-m3) on the persistent .data mount so
    # the cognee `memory` extra doesn't re-download it on every redeploy.
    HF_HOME=/home/app/.data/huggingface
EXPOSE 8080

# Probe the in-container /health route using only the Python stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

ENTRYPOINT ["robotsix-chat"]
