FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROUTINES_FILE=/data/routines.json

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

# Rutinlerin kalıcı olması için volume
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
