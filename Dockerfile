# Use a slim Python base
FROM python:3.11-slim

# Create app dir
WORKDIR /app

# Install system deps if needed (optional)
# RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# Copy files
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Cloud Run listens on $PORT; Flask will use it below
ENV PORT=8080
# Security posture
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Start the Flask app
CMD exec python -m flask --app app run --host=0.0.0.0 --port=$PORT
