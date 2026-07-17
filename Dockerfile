FROM python:3.14-slim

LABEL org.opencontainers.image.title="Magene2Strava" \
      org.opencontainers.image.description="Local web dashboard for syncing Magene/Onelap cycling activities to Strava" \
      org.opencontainers.image.source="https://github.com/sky1wu/Magene2Strava" \
      org.opencontainers.image.licenses="NOASSERTION"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAGENE2STRAVA_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY --chown=app:app download_latest_fit.py sync_to_strava.py web_app.py ./
COPY --chown=app:app web/ ./web/

USER app

VOLUME ["/data"]
EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8848/api/health', timeout=3).read()"]

CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8848"]
