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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.2.0'
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


# ── Relais SMS ───────────────────────────────────────────
# La plateforme publique, dans son état, la liste « smsAFaire » (confirmations, rappels de la veille).
# Le téléphone de chaque personne (raccourci iPhone, MacroDroid) demande « le SMS suivant » qui lui revient,
# l'envoie, puis le déclare envoyé : le registre commun empêche tout double envoi.
PARIS = ZoneInfo('Europe/Paris')
REGISTRE = os.path.join(DATA, 'pf', 'sms-envoyes.json')
_verrou_sms = threading.Lock()


def lire_registre():
    try:
        with open(REGISTRE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def date_iso(x):
    try:
        d = datetime.fromisoformat(str(x).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=PARIS)
    except ValueError:
        return None


def heures_ok(maintenant):
    """Pas de SMS aux clients la nuit : de 8 h à 20 h 30 (heure de Paris)."""
    h = maintenant.astimezone(PARIS)
    return (8, 0) <= (h.hour, h.minute) <= (20, 30)


def file_sms(pour):
    """SMS à envoyer maintenant par cette personne, d'après l'état de plateforme le plus récent."""
    dossier = os.path.join(DATA, 'pf', 'etats')
    fichiers = [os.path.join(dossier, f) for f in os.listdir(dossier) if f.endswith('.json')]
    if not fichiers:
        return []
    try:
        with open(max(fichiers, key=os.path.getmtime), encoding='utf-8') as f:
            items = json.load(f).get('smsAFaire') or []
    except (OSError, ValueError):
        return []
    maintenant, reg, out = datetime.now(timezone.utc), lire_registre(), []
    if not heures_ok(maintenant):
        return []
    for it in items:
        if not isinstance(it, dict) or it.get('pour') != pour or not ID_OK.match(str(it.get('k', ''))) or it['k'] in reg:
            continue
        apres, rdv = date_iso(it.get('apres', '')), date_iso(it.get('rdv', ''))
        if not rdv or rdv <= maintenant + timedelta(hours=2 if it.get('requiert') else 1):
            continue                      # rendez-vous passé ou trop proche
        if apres and apres > maintenant:
            continue                      # pas encore l'heure (rappel : à partir de 16 h la veille)
        req = it.get('requiert')
        if req and not it.get('confirmeManuel'):
            conf = reg.get(req)
            if not conf or (date_iso(conf.get('le', '')) or maintenant) > maintenant - timedelta(hours=12):
                continue                  # rappel seulement si la confirmation est partie depuis au moins 12 h
        if not it.get('tel') or not it.get('texte'):
            continue
        out.append({'k': it['k'], 'tel': it['tel'], 'texte': it['texte']})
    return out


def noter_envoye(k, par):
    with _verrou_sms:
        reg = lire_registre()
        if k not in reg:
            reg[k] = {'le': datetime.now(timezone.utc).isoformat(), 'par': par}
        limite = datetime.now(timezone.utc) - timedelta(days=90)
        reg = {c: v for c, v in reg.items() if (date_iso(v.get('le', '')) or limite) >= limite}
        ecrire(REGISTRE, json.dumps(reg, ensure_ascii=False).encode('utf-8'))


# ── Agenda par abonnement ────────────────────────────────
# Chaque personne a une adresse d'agenda secrète (…/pf/agenda/<id>-<jeton>.ics) : son iPhone, son Mac ou
# Google Agenda s'y abonnent et reçoivent tout seuls ses rendez-vous, mises à jour et annulations comprises.
SECRET_F = os.path.join(DATA, 'secret.key')


def secret():
    try:
        with open(SECRET_F, 'rb') as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = os.urandom(32)
    ecrire(SECRET_F, k)
    os.chmod(SECRET_F, 0o600)
    return k


def jeton(ident):
    return hmac.new(secret(), ('agenda|' + ident).encode(), hashlib.sha256).hexdigest()[:32]


def ics_echap(t):
    return str(t or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\r\n', '\\n').replace('\n', '\\n')


def ics_plier(ligne):
    b, out, cour = ligne.encode('utf-8'), [], b''
    i = 0
    while i < len(b):
        lim = 75 if not out else 74
        j = min(i + lim - len(cour), len(b))
        while j < len(b) and (b[j] & 0xC0) == 0x80:   # ne pas couper un caractère accentué
            j -= 1
        out.append((b' ' if out else b'') + b[i:j])
        i = j
    return b'\r\n'.join(out).decode('utf-8') if out else ''


def ics_date(iso):
    d = date_iso(iso)
    return d.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ') if d else None


def agenda_ics(c, equipe):
    dossier = os.path.join(DATA, 'pf', 'etats')
    fichiers = [os.path.join(dossier, f) for f in os.listdir(dossier) if f.endswith('.json')]
    evs = []
    if fichiers:
        try:
            with open(max(fichiers, key=os.path.getmtime), encoding='utf-8') as f:
                evs = json.load(f).get('agendaRdv') or []
        except (OSError, ValueError):
            evs = []
    noms = {x['id']: x.get('nom', x['id']) for x in comptes()}
    maint = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lignes = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Sublime Toit//Plateforme//FR', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
              'X-WR-CALNAME:' + ics_echap('Sublime Toit – ' + ('équipe' if equipe else 'mes RDV')), 'X-WR-TIMEZONE:Europe/Paris',
              'REFRESH-INTERVAL;VALUE=DURATION:PT15M', 'X-PUBLISHED-TTL:PT15M']
    for e in evs:
        if not isinstance(e, dict) or (not equipe and e.get('pour') != c['id']):
            continue
        deb, fin = ics_date(e.get('debut')), ics_date(e.get('fin'))
        if not deb or not fin or not ID_OK.match(str(e.get('uid', ''))):
            continue
        titre = e.get('titre', 'Rendez-vous')
        if equipe and e.get('pour') != c['id']:
            titre += ' (' + noms.get(e.get('pour'), e.get('pour') or '?').split(' ')[0] + ')'
        lignes += ['BEGIN:VEVENT', 'UID:%s@sublime-toit' % e['uid'], 'DTSTAMP:' + maint, 'DTSTART:' + deb, 'DTEND:' + fin,
                   'SUMMARY:' + ics_echap(titre), 'LOCATION:' + ics_echap(e.get('lieu')), 'DESCRIPTION:' + ics_echap(e.get('notes')),
                   'STATUS:CONFIRMED', 'TRANSP:OPAQUE']
        if not e.get('fait'):
            lignes += ['BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT1H', 'DESCRIPTION:' + ics_echap(titre), 'END:VALARM']
        lignes.append('END:VEVENT')
    lignes.append('END:VCALENDAR')
    return ('\r\n'.join(ics_plier(l) for l in lignes) + '\r\n').encode('utf-8')


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
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS')
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
        if len(parts) == 3 and parts[:2] == ['pf', 'agenda'] and parts[2].endswith('.ics'):
            # Abonnement d'agenda : pas d'en-tête possible, le jeton secret de l'adresse sert de clé
            ip = self.ip()
            if bloque(ip):
                return self.repondre(429, {'erreur': 'Trop d’essais'})
            ident, _, j = parts[2][:-4].rpartition('-')
            c = next((x for x in comptes() if x['id'] == ident), None)
            if not c or not hmac.compare_digest(j, jeton(ident)):
                noter_echec(ip)
                return self.repondre(404, {'erreur': 'Agenda inconnu'})
            self.qui = ident + ' (agenda)'
            equipe = params.get('equipe') == '1' and c.get('role') == 'direction'
            return self.repondre(200, agenda_ics(c, equipe), 'text/calendar; charset=utf-8')
        c = self.compte()
        if not c:
            return
        if parts == ['moi']:
            return self.repondre(200, {'id': c['id'], 'nom': c.get('nom', c['id']), 'role': c.get('role', 'commercial'),
                                       'email': c.get('email', ''), 'tel': c.get('tel', '')})
        if parts == ['equipe']:
            return self.repondre(200, {'equipe': [{k: x.get(k, '') for k in ('id', 'nom', 'role', 'email', 'tel')} for x in comptes()]})
        if parts == ['pf', 'agenda', 'lien']:
            return self.repondre(200, {'chemin': '/pf/agenda/%s-%s.ics' % (c['id'], jeton(c['id'])), 'direction': c.get('role') == 'direction'})
        if parts == ['pf', 'sms', 'suivant']:
            f = file_sms(c['id'])
            return self.repondre(200, f[0] if f else {'rien': True})
        if parts == ['pf', 'sms', 'file']:
            return self.repondre(200, {'sms': file_sms(c['id']), 'heuresOk': heures_ok(datetime.now(timezone.utc))})
        if parts == ['pf', 'sms', 'envoyes']:
            return self.repondre(200, {'envoyes': lire_registre()})
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

    def do_POST(self):
        self.do_PUT()

    def do_PUT(self):
        c = self.compte()
        if not c:
            return
        parts, _ = self.decouper()
        if len(parts) == 4 and parts[:3] == ['pf', 'sms', 'envoye'] and ID_OK.match(parts[3]):
            n = int(self.headers.get('Content-Length') or 0)
            if 0 < n < 100000:
                self.rfile.read(n)
            noter_envoye(parts[3], c['id'])
            return self.repondre(200, {'ok': True})
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
