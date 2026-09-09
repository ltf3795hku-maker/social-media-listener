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
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY streamlit_app.py startup.sh ./
COPY src ./src
COPY .streamlit ./.streamlit

RUN chmod +x startup.sh \
    && mkdir -p /tmp/xhs-data

EXPOSE 8080

CMD ["bash", "startup.sh"]
