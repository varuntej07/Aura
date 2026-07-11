FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (app.py, panels.py, fields.py, providers/, static/). .env / service-account.json
# are gitignored and never copied; Cloud Run injects config via env vars + ADC.
COPY . .

ENV PORT=8080
EXPOSE 8080

# Single worker is plenty for a one-user dashboard; scales to zero between visits.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
