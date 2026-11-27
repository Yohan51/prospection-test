#!/bin/bash
# Copie automatique des sauvegardes de « Sublime Toit – synchro » sur un Mac.
# Installation (dans le Terminal du Mac, sans sudo) :
#   curl -fsSL https://yohan51.github.io/prospection-test/serveur/mac-sauvegarde.sh | bash -s https://sublime-pi.xxxx.ts.net <identifiant>
# Toutes les heures, si la copie du jour n'est pas encore faite et que le Pi répond (Tailscale), le Mac télécharge
# une archive complète dans ~/Sauvegardes Sublime Toit (30 dernières gardées). Le code est rangé dans le trousseau macOS.
set -e
SERVEUR=${1%/}; QUI=${2:-yohan}
[ -z "$SERVEUR" ] && { echo "Usage : … | bash -s https://sublime-pi.xxxx.ts.net <identifiant>"; exit 1; }
DOSSIER="$HOME/Sauvegardes Sublime Toit"
OUTIL="$HOME/Library/Application Support/SublimeToit"
PLIST="$HOME/Library/LaunchAgents/fr.sublime-toit.sauvegarde.plist"
mkdir -p "$DOSSIER" "$OUTIL" "$HOME/Library/LaunchAgents"

echo "▸ Code personnel de « $QUI » (il sera rangé dans le trousseau du Mac, jamais en clair) :"
read -rs CODE < /dev/tty; echo
security delete-generic-password -s sublime-sauvegarde -a "$QUI" >/dev/null 2>&1 || true
security add-generic-password -s sublime-sauvegarde -a "$QUI" -w "$CODE" -U
unset CODE

cat > "$OUTIL/sauvegarde.sh" <<EOS
#!/bin/bash
# Copie de la sauvegarde du Pi (lancé toutes les heures par launchd)
DOSSIER="$DOSSIER"; SERVEUR="$SERVEUR"; QUI="$QUI"
JOUR=\$(date +%Y-%m-%d)
ls "\$DOSSIER"/sublime-\$JOUR*.tar.gz >/dev/null 2>&1 && exit 0          # déjà fait aujourd'hui
CODE=\$(security find-generic-password -s sublime-sauvegarde -a "\$QUI" -w 2>/dev/null) || exit 0
curl -fsS --max-time 20 "\$SERVEUR/" >/dev/null 2>&1 || exit 0          # Pi injoignable : on réessaiera
F="\$DOSSIER/sublime-\$(date +%Y-%m-%d_%H%M).tar.gz"
if curl -fsS --max-time 600 -H "X-Code: \$CODE" "\$SERVEUR/sauvegarde" -o "\$F.part" && gzip -t "\$F.part" 2>/dev/null; then
  mv "\$F.part" "\$F"
  ls -1t "\$DOSSIER"/sublime-*.tar.gz | tail -n +31 | while read -r v; do rm -f "\$v"; done
  echo "\$(date '+%F %T') OK \$(du -h "\$F" | cut -f1)" >> "\$DOSSIER/journal.txt"
else
  rm -f "\$F.part"; echo "\$(date '+%F %T') ÉCHEC" >> "\$DOSSIER/journal.txt"
  DERN=\$(ls -1t "\$DOSSIER"/sublime-*.tar.gz 2>/dev/null | head -1)
  if [ -z "\$DERN" ] || [ \$(( (\$(date +%s) - \$(stat -f %m "\$DERN")) / 86400 )) -ge 3 ]; then
    osascript -e 'display notification "Aucune copie récupérée depuis 3 jours : vérifier Tailscale et le Pi." with title "Sauvegarde Sublime Toit"'
  fi
fi
EOS
chmod 700 "$OUTIL/sauvegarde.sh"

cat > "$PLIST" <<EOS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>fr.sublime-toit.sauvegarde</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$OUTIL/sauvegarde.sh</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$OUTIL/erreurs.log</string>
</dict></plist>
EOS
launchctl bootout gui/$(id -u) "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap gui/$(id -u) "$PLIST"

echo "▸ Premier essai…"
bash "$OUTIL/sauvegarde.sh"
if ls "$DOSSIER"/sublime-*.tar.gz >/dev/null 2>&1; then
  echo "✅ C'est en place. Dernière copie : $(ls -1t "$DOSSIER"/sublime-*.tar.gz | head -1 | xargs -I{} basename {})"
  echo "   Dossier : $DOSSIER (une copie par jour, 30 gardées)"
else
  echo "⚠️ Pas de copie pour l'instant. Journal : $DOSSIER/journal.txt"
  echo "   Vérifier : Tailscale connecté, code juste, et sur le Pi : sudo sublime-comptes admin $QUI"
fi
