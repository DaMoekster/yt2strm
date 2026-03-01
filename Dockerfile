FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV YT2STRM_HOST=0.0.0.0
ENV YT2STRM_PORT=5000
ENV YT2STRM_URL=http://localhost:5000
ENV YT2STRM_MEDIA=/media/YouTube
ENV YT2STRM_DATA=/data
ENV YT2STRM_INTERVAL=3600
ENV YT2STRM_LIMIT=50
ENV YT2STRM_MODE=redirect

EXPOSE 5000

CMD ["python", "app.py"]