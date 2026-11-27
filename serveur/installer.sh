#!/bin/bash
# Installation de « Sublime Toit – synchro » sur un Raspberry Pi (Raspberry Pi OS, Tailscale déjà connecté).
#   curl -fsSL <adresse-du-site>/serveur/installer.sh | sudo bash -s <adresse-du-site>
# Peut être relancé sans risque pour mettre à jour : les données et les comptes sont conservés.
set -e
SITE=${1:-}
[ -z "$SITE" ] && { echo "Usage : curl -fsSL <site>/serveur/installer.sh | sudo bash -s <site>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "À lancer avec sudo."; exit 1; }
SITE=${SITE%/}
APP=/opt/sublime-synchro
DATA=/var/lib/sublime-synchro
echo "▸ Téléchargement des fichiers depuis $SITE/serveur/"
mkdir -p $APP $DATA
for f in serveur.py comptes.py sauvegarde.sh; do
  curl -fsSL "$SITE/serveur/$f?v=$(date +%s)" -o "$APP/$f.tmp" && mv "$APP/$f.tmp" "$APP/$f"
done
chmod 755 $APP/sauvegarde.sh
python3 -c "import ast,sys;ast.parse(open('$APP/serveur.py').read())"

echo "▸ Utilisateur système et droits"
id sublime >/dev/null 2>&1 || useradd --system --home $DATA --shell /usr/sbin/nologin sublime
chown -R sublime:sublime $DATA
chmod 750 $DATA

cat > /usr/local/bin/sublime-comptes <<EOS
#!/bin/bash
[ "\$(id -u)" = 0 ] || { echo "À lancer avec sudo : sudo sublime-comptes \$*"; exit 1; }
SUBLIME_DATA=$DATA python3 $APP/comptes.py "\$@"
chown -R sublime:sublime $DATA
EOS
chmod 755 /usr/local/bin/sublime-comptes

echo "▸ Service (redémarre tout seul, y compris après une coupure de courant)"
cat > /etc/systemd/system/sublime-synchro.service <<EOS
[Unit]
Description=Sublime Toit – synchro
After=network-online.target
Wants=network-online.target

[Service]
User=sublime
Environment=SUBLIME_DATA=$DATA
ExecStart=/usr/bin/python3 -u $APP/serveur.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOS

cat > /etc/systemd/system/sublime-sauvegarde.service <<EOS
[Unit]
Description=Sauvegarde Sublime Toit
[Service]
Type=oneshot
ExecStart=$APP/sauvegarde.sh
EOS
cat > /etc/systemd/system/sublime-sauvegarde.timer <<EOS
[Unit]
Description=Sauvegarde Sublime Toit chaque nuit
[Timer]
OnCalendar=*-*-* 02:30
Persistent=true
[Install]
WantedBy=timers.target
EOS

systemctl daemon-reload
systemctl enable --now sublime-synchro.service sublime-sauvegarde.timer
systemctl restart sublime-synchro.service

echo "▸ Publication en https sur le réseau Tailscale"
tailscale serve --bg --https=443 http://127.0.0.1:8787 || echo "  ⚠️ Si un lien s’affiche ci-dessus, ouvrez-le pour activer « Serve » sur Tailscale, puis relancez cette commande."
NOM=$(tailscale status --json | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
sleep 2
if curl -fsS http://127.0.0.1:8787/ >/dev/null; then echo "  Serveur : OK"; else echo "  ⚠️ Le serveur ne répond pas : sudo journalctl -u sublime-synchro -n 30"; fi

echo
echo "✅ Installation terminée."
echo "   Adresse du serveur à saisir dans l'application : https://$NOM"
if [ ! -s $DATA/comptes.json ]; then
  echo
  echo "   Étape suivante, créer les comptes (chaque personne choisit son code) :"
  echo "     sudo sublime-comptes ajouter yohan \"Yohan Changuion\" commercial"
  echo "     sudo sublime-comptes ajouter david \"David Debouzy\" direction"
fi
