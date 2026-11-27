"""Sublime Toit – synchro : serveur du Raspberry Pi pour la plateforme et sa carte de prospection.

Même principe que « Carte – synchro » : chaque appareil dépose son état complet sous son propre nom,
récupère ceux des autres et fait la fusion lui-même (fiche par fiche, la plus récente gagne).
Rien n'est jamais écrasé par un autre appareil.

Deux espaces séparés :
  /pf/...     la plateforme (prospects, RDV, R1, chantiers…)
  /carte/...  la carte de prospection intégrée (portes, rues, salons…)

Accès : chaque personne a son code personnel (en-tête X-Code), enregistré haché dans comptes.json
(outil : comptes.py). Le serveur n'écoute que sur la machine elle-même : Tailscale le publie en https
pour les seuls appareils du réseau privé.
"""
import hashlib, hmac, json, os, re, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.0.0'
DATA = os.environ.get('SUBLIME_DATA', '/var/lib/sublime-synchro')
COMPTES = os.path.join(DATA, 'comptes.json')
HOTE = os.environ.get('SUBLIME_HOTE', '127.0.0.1')
PORT = int(os.environ.get('SUBLIME_PORT', '8787'))
ID_OK = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
MAX_ETAT = 60 * 1024 * 1024
MAX_PHOTO = 12 * 1024 * 1024
ESPACES = {'pf': 'sublime-toit', 'carte': 'carte-prospection'}
TYPES = {b'\xff\xd8': ('jpg', 'image/jpeg'), b'\x89P': ('png', 'image/png'), b'RI': ('webp', 'image/webp')}
for e in ESPACES:
    os.makedirs(os.path.join(DATA, e, 'etats'), exist_ok=True)
    os.makedirs(os.path.join(DATA, e, 'photos'), exist_ok=True)


# ── Comptes ──────────────────────────────────────────────
def hacher(code, sel=None):
    sel = sel or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'), bytes.fromhex(sel), 120000).hex()
    return f'pbkdf2${sel}${h}'


def code_ok(code, empreinte):
    try:
        _, sel, h = empreinte.split('$')
    except ValueError:
        return False
    return hmac.compare_digest(hacher(code, sel).split('$')[2], h)


_cache_comptes = {'mtime': None, 'liste': []}
_cache_codes = {}   # code → (mtime, compte) : évite de recalculer le hachage à chaque requête


def comptes():
    try:
        m = os.path.getmtime(COMPTES)
    except OSError:
        return []
    if _cache_comptes['mtime'] != m:
        try:
            with open(COMPTES, encoding='utf-8') as f:
                _cache_comptes['liste'] = json.load(f).get('comptes', [])
        except (OSError, ValueError):
            _cache_comptes['liste'] = []
        _cache_comptes['mtime'] = m
        _cache_codes.clear()
    return [c for c in _cache_comptes['liste'] if c.get('actif', True)]


def compte_du_code(code):
    if not code or len(code) > 200:
        return None
    liste = comptes()
    cle = hashlib.sha256(code.encode('utf-8')).hexdigest()
    if cle in _cache_codes and _cache_codes[cle][0] == _cache_comptes['mtime']:
        return _cache_codes[cle][1]
    for c in liste:
        if code_ok(code, c.get('code', '')):
            _cache_codes[cle] = (_cache_comptes['mtime'], c)
            return c
    return None


# Trop d'essais de code faux depuis la même adresse : pause de 5 minutes
_echecs, _verrou = {}, threading.Lock()


def bloque(ip):
    with _verrou:
        t = [x for x in _echecs.get(ip, []) if x > time.time() - 300]
        _echecs[ip] = t
        return len(t) >= 8


def noter_echec(ip):
    with _verrou:
        _echecs.setdefault(ip, []).append(time.time())


# ── Données ──────────────────────────────────────────────
def ecrire(chemin, contenu):
    tmp = chemin + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(contenu)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, chemin)


def sans_sms_passes(etat):
    """Carte : retire des listes « SMS à envoyer » les rendez-vous passés ou à moins d'une heure."""
    items = etat.get('smsAFaire') if isinstance(etat, dict) else None
    if not isinstance(items, list):
        return etat
    limite, garder = time.time() + 3600, []
    for it in items:
        try:
            if it.get('fin'):
                t = datetime.fromisoformat(it['fin'].replace('Z', '+00:00')).timestamp()
            else:
                t = datetime.strptime(it['rdv'], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp() - 2 * 3600
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if t > limite:
            garder.append(it)
    etat['smsAFaire'] = garder
    return etat


class Gestion(BaseHTTPRequestHandler):
    server_version = 'SublimeSynchro/' + VERSION
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print('%s %s' % (getattr(self, 'qui', '-'), (fmt % args).split('?')[0]), flush=True)

    def ip(self):
        return self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()

    def repondre(self, code, corps=b'', type_='application/json', autres=None):
        if isinstance(corps, (dict, list)):
            corps = json.dumps(corps, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'X-Code, Content-Type, If-None-Match')
        self.send_header('Access-Control-Expose-Headers', 'ETag')
        self.send_header('Access-Control-Max-Age', '86400')
        if corps:
            self.send_header('Content-Type', type_)
        self.send_header('Content-Length', str(len(corps)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (autres or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if corps and self.command != 'HEAD':
            self.wfile.write(corps)

    def compte(self):
        ip = self.ip()
        if bloque(ip):
            self.repondre(429, {'erreur': 'Trop d’essais : réessayez dans quelques minutes'})
            return None
        if not comptes():
            self.repondre(503, {'erreur': 'Aucun compte sur le serveur : lancez « sudo sublime-comptes ajouter »'})
            return None
        c = compte_du_code(self.headers.get('X-Code', ''))
        if not c:
            noter_echec(ip)
            time.sleep(0.6)
            self.repondre(401, {'erreur': 'Code incorrect'})
            return None
        self.qui = c['id']
        return c

    def lire_corps(self, maxi):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > maxi:
            self.rfile.read(min(max(n, 0), maxi))
            self.repondre(413 if n > maxi else 400, {'erreur': 'Taille invalide'})
            return None
        return self.rfile.read(n)

    def decouper(self):
        chemin, _, requete = self.path.partition('?')
        parts = [p for p in chemin.split('/') if p]
        params = dict(p.split('=', 1) for p in requete.split('&') if '=' in p)
        return parts, params

    def do_OPTIONS(self):
        self.repondre(204)

    def do_GET(self):
        parts, params = self.decouper()
        if not parts:
            return self.repondre(200, {'service': 'sublime-synchro', 'version': VERSION, 'ok': True})
        c = self.compte()
        if not c:
            return
        if parts == ['moi']:
            return self.repondre(200, {'id': c['id'], 'nom': c.get('nom', c['id']), 'role': c.get('role', 'commercial'),
                                       'email': c.get('email', ''), 'tel': c.get('tel', '')})
        if parts == ['equipe']:
            return self.repondre(200, {'equipe': [{k: x.get(k, '') for k in ('id', 'nom', 'role', 'email', 'tel')} for x in comptes()]})
        if not parts or parts[0] not in ESPACES:
            return self.repondre(404, {'erreur': 'Inconnu'})
        esp, base = parts[0], os.path.join(DATA, parts[0])
        etats, photos = os.path.join(base, 'etats'), os.path.join(base, 'photos')
        if parts[1:] == ['sante']:
            return self.repondre(200, {'ok': True, 'appareils': len(os.listdir(etats))})
        if parts[1:] == ['etats']:
            sauf = params.get('sauf', '')
            fichiers = sorted(f for f in os.listdir(etats) if f.endswith('.json') and f[:-5] != sauf)
            empreinte = hashlib.sha1(('%d|' % (time.time() // 900)).encode() + '|'.join(
                '%s:%d:%d' % (f, os.path.getmtime(os.path.join(etats, f)) * 1000, os.path.getsize(os.path.join(etats, f)))
                for f in fichiers).encode()).hexdigest()
            etag = '"%s"' % empreinte
            if self.headers.get('If-None-Match') == etag:
                return self.repondre(304, autres={'ETag': etag})
            appareils = []
            for f in fichiers:
                try:
                    with open(os.path.join(etats, f), encoding='utf-8') as fh:
                        e = json.load(fh)
                    appareils.append({'id': f[:-5], 'etat': sans_sms_passes(e) if esp == 'carte' else e})
                except (OSError, ValueError):
                    pass
            return self.repondre(200, {'appareils': appareils}, autres={'ETag': etag})
        if parts[1:] == ['photos']:
            return self.repondre(200, {'photos': sorted({f.rsplit('.', 1)[0] for f in os.listdir(photos) if not f.endswith('.tmp')})})
        if len(parts) == 3 and parts[1] == 'photo' and ID_OK.match(parts[2]):
            for ext, typ in TYPES.values():
                f = os.path.join(photos, parts[2] + '.' + ext)
                if os.path.exists(f):
                    with open(f, 'rb') as fh:
                        return self.repondre(200, fh.read(), typ, {'Cache-Control': 'private, max-age=31536000, immutable'})
            return self.repondre(404, {'erreur': 'Photo absente'})
        self.repondre(404, {'erreur': 'Inconnu'})

    def do_PUT(self):
        c = self.compte()
        if not c:
            return
        parts, _ = self.decouper()
        if len(parts) != 3 or parts[0] not in ESPACES or not ID_OK.match(parts[2]):
            return self.repondre(404, {'erreur': 'Inconnu'})
        esp, base = parts[0], os.path.join(DATA, parts[0])
        if parts[1] == 'etat':
            corps = self.lire_corps(MAX_ETAT)
            if corps is None:
                return
            try:
                etat = json.loads(corps)
                assert etat.get('app') == ESPACES[esp]
                if esp == 'carte':
                    assert isinstance(etat.get('portes'), list)
            except (ValueError, AssertionError, AttributeError):
                return self.repondre(400, {'erreur': 'Données illisibles'})
            ecrire(os.path.join(base, 'etats', parts[2] + '.json'), corps)
            return self.repondre(200, {'ok': True})
        if parts[1] == 'photo':
            corps = self.lire_corps(MAX_PHOTO)
            if corps is None:
                return
            t = TYPES.get(corps[:2])
            if not t or (t[0] == 'webp' and corps[8:12] != b'WEBP'):
                return self.repondre(400, {'erreur': 'Image non reconnue'})
            ecrire(os.path.join(base, 'photos', parts[2] + '.' + t[0]), corps)
            return self.repondre(200, {'ok': True})
        self.repondre(404, {'erreur': 'Inconnu'})

    def do_DELETE(self):
        c = self.compte()
        if not c:
            return
        parts, params = self.decouper()
        if len(parts) == 3 and parts[0] in ESPACES and parts[1:] == ['etats', 'anciens']:
            if c.get('role') != 'direction' and parts[0] == 'pf':
                return self.repondre(403, {'erreur': 'Réservé à la direction'})
            try:
                jours = max(1.0, float(params.get('jours', '30')))
            except ValueError:
                jours = 30.0
            limite, supprimes = time.time() - jours * 86400, []
            dossier = os.path.join(DATA, parts[0], 'etats')
            for f in os.listdir(dossier):
                p = os.path.join(dossier, f)
                if f.endswith('.json') and f != 'relais-sms.json' and os.path.getmtime(p) < limite:
                    os.remove(p)
                    supprimes.append(f[:-5])
            return self.repondre(200, {'ok': True, 'supprimes': supprimes})
        self.repondre(404, {'erreur': 'Inconnu'})


if __name__ == '__main__':
    print('Sublime Toit – synchro %s sur %s:%d, %d compte(s), données dans %s' % (VERSION, HOTE, PORT, len(comptes()), DATA), flush=True)
    ThreadingHTTPServer((HOTE, PORT), Gestion).serve_forever()
