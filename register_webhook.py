"""Enregistre l'URL du tunnel cloudflared comme webhook WhatsApp chez Meta.

Un « quick tunnel » change d'adresse à chaque démarrage : sans ce script,
Meta continue d'envoyer les messages à l'ancienne adresse et le bot ne
répond plus. Lancé par le service « register » de docker-compose.yml avec
--watch : il ré-enregistre l'adresse chaque fois que le tunnel redémarre.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
WEBHOOK_PATH = '/api/whatsapp/webhook/'
# cloudflared expose l'adresse du tunnel sur son port « metrics ». Dans Docker,
# TUNNEL_METRICS_URL pointe vers le service « tunnel » ; hors Docker, on
# cherche sur les ports locaux par défaut (20241 à 20245).
METRICS_URLS = (
    [os.environ['TUNNEL_METRICS_URL'].rstrip('/')]
    if os.environ.get('TUNNEL_METRICS_URL')
    else [f'http://127.0.0.1:{port}' for port in range(20241, 20246)]
)
DEFAULT_APP_ID = '1128374806418296'


def load_env():
    """Variables d'environnement, complétées par le fichier .env s'il existe."""
    env = dict(os.environ)
    env_file = BASE_DIR / '.env'
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        env.setdefault(key.strip(), value.strip().strip('"\''))
    return env


def http(url, data=None, timeout=15):
    """Retourne (code HTTP ou None, corps de la réponse)."""
    body = urlencode(data).encode('utf-8') if data else None
    try:
        with urlopen(Request(url, data=body), timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')
    except (URLError, OSError) as exc:
        return None, str(exc)


def lire_tunnel():
    """Retourne l'adresse actuelle du tunnel, ou None s'il n'est pas prêt."""
    for metrics_url in METRICS_URLS:
        status, body = http(f'{metrics_url}/quicktunnel', timeout=2)
        if status == 200:
            hostname = json.loads(body).get('hostname')
            if hostname:
                return hostname
    return None


def attendre_tunnel(tentatives=30):
    for _ in range(tentatives):
        hostname = lire_tunnel()
        if hostname:
            return hostname
        time.sleep(2)
    return None


def attendre_webhook(callback_url, verify_token, tentatives=30):
    """Attend que le webhook réponde à travers le tunnel (le DNS met quelques secondes)."""
    query = urlencode({
        'hub.mode': 'subscribe', 'hub.verify_token': verify_token, 'hub.challenge': 'ping',
    })
    for _ in range(tentatives):
        status, body = http(f'{callback_url}?{query}', timeout=10)
        if status == 200 and body == 'ping':
            return True
        time.sleep(2)
    return False


def enregistrer(env, hostname):
    """Enregistre le tunnel chez Meta. Retourne True si Meta a accepté."""
    app_id = env.get('WHATSAPP_APP_ID', DEFAULT_APP_ID)
    app_secret = env['WHATSAPP_APP_SECRET']
    verify_token = env['WHATSAPP_VERIFY_TOKEN']
    callback_url = f'https://{hostname}{WEBHOOK_PATH}'
    print(f'Tunnel : {callback_url}')

    print('Attente du serveur...')
    if not attendre_webhook(callback_url, verify_token):
        print('[ERREUR] Le serveur ne repond pas a travers le tunnel.')
        return False

    api_version = env.get('WHATSAPP_API_VERSION', 'v23.0')
    status, body = None, ''
    for _ in range(5):
        status, body = http(
            f'https://graph.facebook.com/{api_version}/{app_id}/subscriptions',
            data={
                'object': 'whatsapp_business_account',
                'callback_url': callback_url,
                'verify_token': verify_token,
                'fields': 'messages',
                'access_token': f'{app_id}|{app_secret}',
            },
        )
        if status == 200:
            print('[OK] Adresse enregistree chez Meta. Le bot WhatsApp est pret.')
            return True
        time.sleep(3)
    print(f'[ERREUR] Meta a refuse l\'enregistrement (HTTP {status}) : {body[:400]}')
    return False


def surveiller(env):
    """Ré-enregistre l'adresse chaque fois que le tunnel en change."""
    enregistre = None
    while True:
        hostname = lire_tunnel()
        if hostname and hostname != enregistre and enregistrer(env, hostname):
            enregistre = hostname
        time.sleep(30 if hostname and hostname == enregistre else 5)


def main():
    env = load_env()
    if not env.get('WHATSAPP_APP_SECRET') or not env.get('WHATSAPP_VERIFY_TOKEN'):
        print('[ERREUR] WHATSAPP_APP_SECRET ou WHATSAPP_VERIFY_TOKEN manquant dans .env')
        return 1

    if '--watch' in sys.argv[1:]:
        surveiller(env)

    print('Recherche de l\'adresse du tunnel...')
    hostname = attendre_tunnel()
    if not hostname:
        print('[ERREUR] Tunnel introuvable. Est-il demarre ?')
        return 1
    return 0 if enregistrer(env, hostname) else 1


if __name__ == '__main__':
    sys.exit(main())
