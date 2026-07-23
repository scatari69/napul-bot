FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
