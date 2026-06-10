# Build context: repo root (set by docker-compose.yml `context: .`).
FROM python:3.11-slim

WORKDIR /app

# Install beam (production deps only — no dev extras).
COPY backend/pyproject.toml ./
COPY backend/beam/ ./beam/
RUN pip install --no-cache-dir -e .

# Config and scenario presets. config_dir() in beam/config.py resolves to
# /config (three parents up from /app/beam/config.py = /).
COPY config/ /config

EXPOSE 8000

CMD ["beam", "serve", "--host", "0.0.0.0", "--port", "8000"]
