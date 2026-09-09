# Cloud Run image for the Streamlit app.
# Cloud Run injects PORT (usually 8080); startup.sh already reads it.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    XHS_DATA_DIR=/tmp/xhs-data \
    UI_PREVIEW_MODE=false \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY streamlit_app.py startup.sh ./
COPY src ./src
COPY .streamlit ./.streamlit

RUN chmod +x startup.sh \
    && mkdir -p /tmp/xhs-data

EXPOSE 8080

CMD ["bash", "startup.sh"]
