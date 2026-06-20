FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest.py .

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

# session.session, .env, telegraph_token.txt, and html/ are mounted at runtime
# see docker-compose.yml or the run command in README

# Echo the image identity to stdout (captured in digest.log via cron), then run.
CMD echo "Docker image: tag=${DIGEST_IMAGE_TAG} hash=${DIGEST_IMAGE_HASH:-unknown} branch=${DIGEST_GIT_BRANCH} commit=${DIGEST_GIT_COMMIT}" && exec python digest.py
