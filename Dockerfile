# Baileys needs node >= 20; bookworm's apt nodejs is 18, so the runtime comes
# from the official node image rather than apt.
FROM node:22-slim AS wa
WORKDIR /wa
COPY scripts/wa/package.json scripts/wa/package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest.py .

# Optional WhatsApp delivery. Inert unless WHATSAPP_CHANNEL_JID is set, but node
# and the Baileys deps have to be in the image for that switch to do anything.
# The node binary links against libstdc++, which the slim image does not carry.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wa /usr/local/bin/node /usr/local/bin/node
COPY --from=wa /wa/node_modules ./scripts/wa/node_modules
COPY scripts/wa/send.js ./scripts/wa/

# Build-time image identity, supplied by build.sh (which also refuses to build
# from a dirty tree). The real image sha256 ID cannot be baked in (it is
# computed after the build), so run.sh injects DIGEST_IMAGE_HASH at runtime via
# `docker image inspect`.
ARG IMAGE_TAG=unknown
ARG GIT_BRANCH=unknown
ARG GIT_COMMIT=unknown
ENV DIGEST_IMAGE_TAG=${IMAGE_TAG} \
    DIGEST_GIT_BRANCH=${GIT_BRANCH} \
    DIGEST_GIT_COMMIT=${GIT_COMMIT}

# session.session, .env, telegraph_token.txt, html/, and (for WhatsApp)
# scripts/wa/auth/ are mounted at runtime
# see docker-compose.yml or the run command in README

# Echo the image identity to stdout (captured in digest.log via cron), then run.
CMD echo "Docker image: tag=${DIGEST_IMAGE_TAG} hash=${DIGEST_IMAGE_HASH:-unknown} branch=${DIGEST_GIT_BRANCH} commit=${DIGEST_GIT_COMMIT}" && exec python digest.py
