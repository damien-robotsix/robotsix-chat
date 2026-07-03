# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage: resolve the locked dependency set and export a requirements.txt
# for installation in the runtime stage.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061 AS builder

# Bring in the uv static binary (pinned to a released version for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

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
# for Langfuse observability + memory for cognee + broker for agent-comm).
RUN uv export --frozen --no-emit-project --no-hashes --extra claude-sdk --extra tracing --extra memory --extra broker > requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage: install locked deps + project into the system Python, then
# add Node.js + the claude CLI, running as a non-root user.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061 AS runtime

# Bring in uv and the exported requirements for the --system install.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
COPY --from=builder /app/requirements.txt /tmp/requirements.txt

# Copy project source (needed for the --no-deps self-install; removed after).
COPY pyproject.toml uv.lock README.md /tmp/project/
COPY src /tmp/project/src

# Install git temporarily so uv can fetch the git-sourced dependencies
# (robotsix-yaml-config, robotsix-llmio), install the locked deps and the
# project into the system site-packages, then remove git, uv, and the
# transient source to keep the runtime layer lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && uv pip install --system -r /tmp/requirements.txt \
    && uv pip install --system --no-deps /tmp/project \
    && apt-get purge -y --auto-remove git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt /tmp/project /usr/local/bin/uv

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
