FROM python:3.11-slim

# Create a non-root user
RUN groupadd --gid 1001 arypodr \
    && useradd --uid 1001 --gid arypodr --shell /bin/bash --create-home arypodr

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY api/ ./api/

RUN chown -R arypodr:arypodr /app \
    && mkdir -p /podcasts \
    && chown arypodr:arypodr /podcasts

USER arypodr

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
