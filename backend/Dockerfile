# OfferFlow AI backend image (local development).
# Python 3.12 pairs with Django 5.2 LTS, DRF, Celery 5.6 and psycopg 3
# (compatibility verified 30 Oct 2026).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /var/offerflow/files \
    && chown app:app /var/offerflow/files

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN chown -R app:app /app

USER app

EXPOSE 8000

# Development default; Compose overrides per service (API server vs worker).
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
