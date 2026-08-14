# Dockerfile — Data Resonance POC
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt || \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create storage directories
RUN mkdir -p storage/uploads storage/outputs storage/contracts storage/kb

# Expose Flask port
EXPOSE 5000

# Run the app
CMD ["python", "main.py"]