FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TESSERACT_CMD=/usr/bin/tesseract

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-pol \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Dodajemy bot.py do plików kopiowanych do kontenera
COPY app.py bot.py init_db.py regulamin.html ./
COPY templates ./templates

EXPOSE 10000

# Zmodyfikowana komenda startowa: najpierw odpala bot.py w tle (&), a potem serwer WWW przez gunicorn
CMD ["sh", "-c", "python -c 'from app import init_advanced_db; init_advanced_db()' && exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000}"]
