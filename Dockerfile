FROM python:3.13-slim

# PLAYWRIGHT_BROWSERS_PATH installs browsers to a shared, world-readable path so
# the non-root `hunter` runtime user finds them (root's ~/.cache would not be
# readable, causing "Executable doesn't exist at .../ms-playwright/...").
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && playwright install chromium --with-deps \
    && chmod -R a+rX /ms-playwright

COPY . .

# Create non-root user and dirs
RUN useradd -m -u 1000 hunter \
    && mkdir -p screenshots config backups \
    && chown -R hunter:hunter /app

USER hunter

VOLUME ["/app/config", "/app/screenshots"]

ENTRYPOINT ["python", "main.py"]
# bot-all runs one supervised bot process per profile in HUNTER_PROFILES (set in
# fly.toml). With HUNTER_PROFILES unset it's just the single default bot, so this
# is safe for a single-profile deploy too.
CMD ["bot-all"]
