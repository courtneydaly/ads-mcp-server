# Streamable-HTTP deployment (e.g. Cloud Run) against the BigQuery marts.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ads_mcp ./ads_mcp
RUN pip install --no-cache-dir ".[bigquery]"
ENV ADS_BACKEND=bigquery
EXPOSE 8080
CMD ["python", "-m", "ads_mcp.server", "--http", "8080"]
