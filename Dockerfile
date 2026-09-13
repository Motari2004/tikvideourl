FROM python:3.11-slim

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.cache/playwright \
    PORT=10000

# ---------------------------------------------------------------------------
# System dependencies for Chromium (Playwright)
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    wget \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------
WORKDIR /app

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Playwright browser (Chromium only)
# ---------------------------------------------------------------------------
RUN playwright install chromium

# ---------------------------------------------------------------------------
# App code
# ---------------------------------------------------------------------------
COPY . .

# Ensure runtime directories exist and are writable
RUN mkdir -p /app/downloads /app/logs \
    && chmod -R 777 /app/downloads /app/logs

# ---------------------------------------------------------------------------
# Expose port (Render uses 10000 by convention)
# ---------------------------------------------------------------------------
EXPOSE 10000

# ---------------------------------------------------------------------------
# Run with gunicorn
#   --workers 1     -> serialize Playwright (matches FETCH_LOCK in app.py)
#   --threads 4     -> allow concurrent HTTP requests
#   --timeout 300   -> allow slow Playwright fetches to finish
# ---------------------------------------------------------------------------
CMD ["gunicorn", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"] 
