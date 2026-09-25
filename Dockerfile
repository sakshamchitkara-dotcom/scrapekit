# scrapekit is stdlib-only; PyYAML is included so YAML recipes work too.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY scrapekit ./scrapekit
COPY recipes ./recipes
RUN pip install --no-cache-dir '.[yaml]' \
    && useradd --create-home scrapekit \
    && mkdir /data && chown scrapekit /data

USER scrapekit
# Mount a volume here to keep the database between runs.
WORKDIR /data
ENTRYPOINT ["scrapekit"]
CMD ["--help"]
