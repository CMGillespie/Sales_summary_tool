FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY matcher_gcp.py .

ENV PYTHONUNBUFFERED=1

CMD ["python3", "matcher_gcp.py"]
