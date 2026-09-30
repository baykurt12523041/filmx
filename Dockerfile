FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY api.py telegram_bot.py .

# Expose the default FastAPI port
EXPOSE 8000

# Run API + Telegram bot together:
# - uvicorn arkada API'yi acar
# - bot on planda polling yapar (container ayakta tutan process)
# Koyeb sets PORT environment variable, so we use it with a default of 8000
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} & export API_BASE_URL=http://127.0.0.1:${PORT:-8000}; python telegram_bot.py"]
