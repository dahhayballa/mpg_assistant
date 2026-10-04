FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Les dépendances d'abord : cette couche reste en cache tant que
# requirements.txt ne change pas.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Les fichiers statiques sont servis par WhiteNoise depuis l'image.
RUN python manage.py collectstatic --noinput

# Ne pas faire tourner l'application en root.
RUN useradd --create-home --uid 1000 mpg && chown -R mpg:mpg /app
USER mpg

EXPOSE 8000

# Même séquence de démarrage que sur Render (voir render.yaml).
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py import_docx_documents && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60"]
