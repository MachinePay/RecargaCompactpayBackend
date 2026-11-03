FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Uma instancia so, sem --workers: as conexoes OCPP ficam em memoria.
# A Render passa a porta em $PORT; local cai na 8000. --proxy-headers para
# enxergar o https/wss e o IP real atras do proxy dela.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
