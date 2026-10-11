FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --uid 1000 --create-home app
COPY categories.yaml ./

USER app
ENTRYPOINT ["inbox-jev"]
CMD ["--help"]
