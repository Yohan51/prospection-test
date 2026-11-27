"""Gestion des comptes de « Sublime Toit – synchro ».

  sudo sublime-comptes liste
  sudo sublime-comptes ajouter <identifiant> "<Prénom NOM>" <direction|commercial|technique|operateur> [email]
  sudo sublime-comptes code <identifiant>        (change le code d'une personne)
  sudo sublime-comptes desactiver <identifiant>  (téléphone perdu, départ…)
  sudo sublime-comptes activer <identifiant>
  sudo sublime-comptes admin <identifiant>       (autorise la copie automatique des sauvegardes sur son Mac)

Les codes ne sont jamais enregistrés en clair : seul leur hachage est gardé dans comptes.json.
"""
import getpass, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serveur import COMPTES, hacher

ROLES = ('direction', 'commercial', 'technique', 'operateur')


def lire():
    try:
        with open(COMPTES, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'comptes': []}


def ecrire(d):
    tmp = COMPTES + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    os.replace(tmp, COMPTES)


def demander_code(existants):
    while True:
        c1 = getpass.getpass('Code personnel (8 caractères minimum, chiffres et/ou lettres) : ')
        if len(c1) < 8:
            print('  Trop court : 8 caractères minimum.')
            continue
        if any(__import__('serveur').code_ok(c1, x.get('code', '')) for x in existants):
            print('  Ce code est déjà utilisé par quelqu’un d’autre : choisissez-en un autre.')
            continue
        if getpass.getpass('Retapez le code : ') != c1:
            print('  Les deux codes ne correspondent pas.')
            continue
        return hacher(c1)


def main(a):
    d = lire()
    cs = d.setdefault('comptes', [])
    trouve = lambda i: next((c for c in cs if c['id'] == i), None)
    if not a or a[0] in ('aide', '-h', '--help'):
        print(__doc__)
        return
    if a[0] == 'liste':
        if not cs:
            print('Aucun compte.')
        for c in cs:
            print(f"{c['id']:<14} {c.get('nom', ''):<26} {c.get('role', ''):<11} {'actif' if c.get('actif', True) else 'DÉSACTIVÉ'}")
        return
    if a[0] == 'ajouter' and len(a) >= 4:
        i, nom, role = a[1].lower(), a[2], a[3].lower()
        if not re.match(r'^[a-z0-9_-]{2,30}$', i):
            sys.exit('Identifiant : lettres minuscules, chiffres, - ou _ (ex. : yohan, david).')
        if role not in ROLES:
            sys.exit('Rôle : ' + ', '.join(ROLES))
        if trouve(i):
            sys.exit(f'Le compte « {i} » existe déjà (utilisez « code {i} » pour changer son code).')
        cs.append({'id': i, 'nom': nom, 'role': role, 'email': a[4] if len(a) > 4 else '', 'code': demander_code(cs), 'actif': True})
        ecrire(d)
        print(f'Compte « {i} » créé ({nom}, {role}).')
        return
    if a[0] == 'admin' and len(a) == 2:
        c = trouve(a[1].lower())
        if not c:
            sys.exit(f'Pas de compte « {a[1]} ».')
        c['admin'] = True
        ecrire(d)
        print(f'« {c["id"]} » peut maintenant récupérer les sauvegardes.')
        return
    if a[0] in ('code', 'desactiver', 'activer') and len(a) == 2:
        c = trouve(a[1].lower())
        if not c:
            sys.exit(f'Pas de compte « {a[1]} ».')
        if a[0] == 'code':
            c['code'] = demander_code([x for x in cs if x is not c])
        else:
            c['actif'] = a[0] == 'activer'
        ecrire(d)
        print('C’est fait.')
        return
    print(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
