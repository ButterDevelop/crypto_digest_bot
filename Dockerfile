# --- Build stage ---
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    python3-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    libcairo2-dev \
    libpango1.0-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# --- Final stage ---
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for WeasyPrint and PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python packages
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy project files
COPY . .

# Create data directory for persistent SQLite storage
RUN mkdir -p /app/data

# Environment defaults
ENV DB_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
