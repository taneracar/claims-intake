FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PREFECT_HOME=/tmp/prefect \
    PREFECT_SERVER_ANALYTICS_ENABLED=false

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home app

# Inputs and outputs live in a mounted volume; nothing patient-related is baked into the image.
WORKDIR /work
USER 10001
ENTRYPOINT ["claims-intake"]
CMD ["--help"]
