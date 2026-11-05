FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY scripts/build.mjs ./scripts/
COPY tests/test_player.mjs ./tests/
COPY web/app.js web/style.css ./web/
RUN npm run build && npm test

FROM python:3.13-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0 \
    PORT=8080 \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/.cache/deno
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --user-group syncnest
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt LICENSE ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY web/index.html ./web/
COPY --from=frontend /build/web/dist ./web/dist
USER syncnest
EXPOSE 8080
STOPSIGNAL SIGINT
CMD ["python", "-m", "syncnest.bot"]

FROM runtime AS tests
COPY benchmarks/ ./benchmarks/
COPY tests/ ./tests/
RUN python -m unittest discover -v

FROM runtime AS production
