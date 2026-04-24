FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY app/ ./app/
COPY templates/ ./templates/

RUN mkdir -p /app/data /app/cookies

ENTRYPOINT ["/entrypoint.sh"]
