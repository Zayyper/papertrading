# hl-niche-screener: web page + paper trader. One image, two services (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.toml README.md ./
COPY hl_screener ./hl_screener
COPY design ./design
COPY paper ./paper

# runs, cache and the paper database live on volumes so they survive redeploys
RUN mkdir -p /app/data /app/out
VOLUME ["/app/data", "/app/out"]

EXPOSE 8765
CMD ["python", "-m", "hl_screener", "ui", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
