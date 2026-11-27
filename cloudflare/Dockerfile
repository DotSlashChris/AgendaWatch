# Agenda Watch — Cloudflare Containers image. Build context = repo root (see wrangler.jsonc image_build_context).
FROM python:3.12-slim
LABEL aw.build="20261027-minutes-labeled"
ENV AW_BUILD=20261127-ref-attribution
RUN rm -rf /var/lib/apt/lists/* && apt-get -o Acquire::EnableSrvRecords=false update && apt-get -o Acquire::EnableSrvRecords=false install -y --no-install-recommends poppler-utils ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 TZ=America/Denver AW_CLOUD=1
WORKDIR /app
COPY requirements-web.txt requirements-run.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt -r requirements-run.txt
COPY agenda_watch.py aw_alerts.py aw_cities.py aw_city_sources.py aw_guard.py aw_store.py config.json ./
COPY web/ ./web/
COPY cloudflare/persist.py cloudflare/entrypoint.sh ./cloudflare/
# build-time smoke test: the daily job must import (bs4/requests/pdftotext present)
RUN python -c "import agenda_watch, aw_store, aw_alerts, aw_guard, aw_city_sources" && pdftotext -v 2>&1 | head -1
RUN mkdir -p data state outbox cache && chmod +x cloudflare/entrypoint.sh \
    && find web -name '*.bak*' -delete
EXPOSE 8080
CMD ["./cloudflare/entrypoint.sh"]
