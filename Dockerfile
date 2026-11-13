FROM python:3.12-slim

# ffmpeg for merging/converting; deno is the JS runtime yt-dlp uses for YouTube.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

# Unprivileged user that owns its own venv, so YTDLP_AUTO_UPDATE can pip-upgrade.
RUN useradd --create-home --uid 1000 app \
 && python -m venv /opt/venv && chown -R app:app /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/app/data \
    DOWNLOAD_DIR=/app/downloads

WORKDIR /app
COPY --chown=app:app requirements.txt .
USER app
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app app ./app
RUN mkdir -p /app/data /app/downloads

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os, sys, urllib.request, base64; p=os.getenv('APP_PASSWORD'); h={'Authorization':'Basic '+base64.b64encode((':'+p).encode()).decode()} if p else {}; sys.exit(0 if urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/api/health', headers=h), timeout=4).status == 200 else 1)"
CMD ["python", "-m", "app"]
