FROM python:3.12-slim
# ffmpeg: audio probing and transcoding. chromium + DejaVu fonts (Polish diacritics): PDF report output
# (EW_PDF_CHROMIUM; the printed HTML has no script or external assets; root runs it with --no-sandbox).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg chromium fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV EW_PDF_CHROMIUM=/usr/bin/chromium
WORKDIR /app
# requirements-scanner.txt: openpyxl/xlrd for XLSX/XLS import, plan probes and XLSX report output.
COPY requirements.txt requirements-whisper.txt requirements-scanner.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-whisper.txt -r requirements-scanner.txt
COPY . .
EXPOSE 8080
# The client address is the socket peer, never X-Forwarded-For (legacy_open trusts local peers; review 3).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
