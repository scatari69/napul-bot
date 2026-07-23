FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
# База часовых поясов: без нее TZ из compose молча игнорируется и время остается UTC
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
