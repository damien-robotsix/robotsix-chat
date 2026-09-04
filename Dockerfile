# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage: install the locked dependency set + the project into the
# system interpreter (/usr/local), exactly what the runtime stage copies.
# Standard robotsix Dockerfile pattern — see robotsix-standards, docker page.
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:83ff1d245a3d57d04152252d3ef9cb361494d0b3395abd65a5ebe91c401c8e83 AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Bring in uv, pinned to a released version for reproducibility.  Installed
# from the official PyPI wheel rather than COPY --from=ghcr.io/astral-sh/uv:
# the push build authenticates to GHCR with GITHUB_TOKEN, whose packages scope
# is repo-local, so it cannot pull the cross-namespace astral-sh/uv image
# ("failed to fetch oauth token: denied: denied").
RUN python -m pip install --no-cache-dir "uv==0.11.21"

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
# the LLM transport, tracing for Langfuse observability.
# --no-hashes: the git-sourced first-party deps cannot carry hashes.
# hadolint ignore=DL3066
RUN uv export --frozen --no-emit-project --no-hashes \
        --extra claude-sdk --extra tracing --extra render-url \
        --extra github-actions \
        -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && python -c "import nacl.public; print('PyNaCl OK')" \
    && rm -f /tmp/requirements.txt

# ---------------------------------------------------------------------------
# UI stage: fetch the shared @robotsix/ui settings renderer build (vanilla JS
# + CSS, no bundler required) so it can be injected into site-packages below.
# `npm install` of a `github:` dependency fails with a deterministic "Tracker
# 'idealTree' already exists" error when run from the filesystem root (/), so
# install from a real project directory (/build) instead.
# ---------------------------------------------------------------------------
FROM node:26-alpine AS ui
ARG ROBOTSIX_UI_VERSION=v0.1.41
WORKDIR /build
# hadolint ignore=DL3016,DL3018
RUN apk add --no-cache git && \
    npm install --no-save --progress=false "github:damien-robotsix/robotsix-ui#${ROBOTSIX_UI_VERSION}" && \
    test -f node_modules/@robotsix/ui/dist/vanilla.js && \
    test -f node_modules/@robotsix/ui/dist/style.css

# ---------------------------------------------------------------------------
# Runtime stage: copy the installed site-packages and console script from the
# builder — no uv, no git, no compilers. Node.js + the claude CLI are the one
# genuine runtime system dependency (claude-sdk transport spawns the CLI).
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:83ff1d245a3d57d04152252d3ef9cb361494d0b3395abd65a5ebe91c401c8e83 AS runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
RUN mkdir -p /usr/local/lib/python3.14/site-packages/robotsix_chat/ui/static/vendor
COPY --from=ui /build/node_modules/@robotsix/ui/dist/vanilla.js \
  /usr/local/lib/python3.14/site-packages/robotsix_chat/ui/static/vendor/vanilla.js
COPY --from=ui /build/node_modules/@robotsix/ui/dist/style.css \
  /usr/local/lib/python3.14/site-packages/robotsix_chat/ui/static/vendor/style.css
COPY --from=builder /usr/local/bin/robotsix-chat /usr/local/bin/robotsix-chat
COPY --from=builder /usr/local/bin/playwright /usr/local/bin/playwright

# The runtime stage re-inherits pip from the base image (the COPY above
# merges into site-packages rather than replacing it). pip is build-time
# tooling only, and its vendored msgpack/setuptools trip the Trivy gate —
# drop it from the runtime image.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
           /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
           /usr/local/bin/pip*

# Install Node.js (LTS) and the claude CLI — required at runtime: the
# claude-sdk subscription transport spawns the `claude` CLI as a subprocess.
# Build-only packages and caches are pruned in the same layer.
#
# The --only-upgrade list pulls trixie-security builds of packages the base
# image still carries at the older trixie/main version. The openssl trio
# (one source package, three binaries — all three must move together or the
# scan still flags the laggard) covers CVE-2026-14456: the base image ships
# 3.5.6-1~deb13u2, trixie-security has 3.5.7-1~deb13u2.
RUN apt-get update \
    && apt-get install --only-upgrade -y --no-install-recommends \
        liblzma5="5.8.*" \
        util-linux="2.41.*" \
        libblkid1="2.41.*" \
        libmount1="2.41.*" \
        libsmartcols1="2.41.*" \
        libuuid1="2.41.*" \
        libssl3t64="3.5.*" \
        openssl="3.5.*" \
        openssl-provider-legacy="3.5.*" \
    && apt-get install -y --no-install-recommends curl="8.*" gnupg="2.*" \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs="24.*" \
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
USER 1000

EXPOSE 8080

# Probe the in-container /health route using only the Python stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"]

ENTRYPOINT ["robotsix-chat"]
