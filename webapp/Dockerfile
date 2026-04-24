FROM python:3.11-slim

# Install ADB (android-tools) and other dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY webapp/ .

# Copy scrcpy files for Quick Download package (Windows binaries served to clients)
COPY scrcpy/scrcpy-win64-v3.2/ /app/scrcpy/

# Copy client_agent files
COPY client_agent/ /app/client_agent/

# Create directories for persistent data
RUN mkdir -p screenshots reports documents

# Expose default port
EXPOSE 8000

# Environment variable defaults
ENV HOST=0.0.0.0
ENV PORT=8000
ENV ADB_PATH=/usr/bin/adb

CMD ["python", "main.py"]
