FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime -- default it for local `docker run` testing.
ENV PORT=8000
EXPOSE 8000

# Shell form (not exec/JSON form) so $PORT actually gets expanded.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
