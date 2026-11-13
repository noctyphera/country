FROM python:3.12-slim

ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GAME_CONFIG_DIR=/app/config
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY game ./game
RUN pip install -c requirements.lock . && useradd --create-home --uid 10001 countrybot
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations
USER countrybot
CMD ["python", "-m", "game"]
