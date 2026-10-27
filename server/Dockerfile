FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-whisper.txt
COPY . .
EXPOSE 8080
# The client address is the socket peer, never X-Forwarded-For (legacy_open trusts local peers; review 3).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
