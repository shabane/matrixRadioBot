FROM python:3.13-slim

# Install ffmpeg and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt

COPY bot/ ./bot/
COPY config.yaml .

CMD ["python", "-m", "bot"]
