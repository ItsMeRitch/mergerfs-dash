# Tiny image: the app is stdlib-only, so there is nothing to install.
FROM python:3-alpine

ENV PYTHONUNBUFFERED=1 \
    PORT=8282 \
    HOST=0.0.0.0 \
    CACHE_PATH=/data/mergerfs_dash_cache.json

WORKDIR /app
COPY mergerfs_dash.py .

# Run as an unprivileged user; the scan cache lives in /data (a volume).
RUN adduser -D -u 1000 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

EXPOSE 8282

# Everything else (BRANCHES, PORT, ...) is configured via environment
# variables at runtime — see compose.example.yml.
CMD ["python3", "mergerfs_dash.py"]
