# Ingestion now writes to a live Postgres instance, so it can't run at build time
# (build environments don't have your runtime DATABASE_URL, by design). Run
# `python ingest.py` as a one-off command after this service is deployed instead —
# see the README's "Deploying to Railway" section.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime — default it for local `docker run` testing.
ENV PORT=8000
EXPOSE 8000

# Shell form (not exec/JSON form) so $PORT actually gets expanded.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
