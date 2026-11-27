#!/bin/bash
# Sauvegarde de chaque nuit : archive datée des données, sur la clé USB si elle est branchée
# (montée le temps de la copie), sinon dans /var/backups/sublime-synchro. On garde les 30 dernières.
set -e
SRC=/var/lib/sublime-synchro
POINT=/mnt/cle-sauvegarde
RACINE=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null || true)
CLE=""
for p in $(lsblk -rpno NAME,TYPE | awk '$2=="part" && $1 ~ /\/dev\/sd/ {print $1}'); do
  disque=$(lsblk -no PKNAME "$p")
  [ "$disque" = "$RACINE" ] && continue          # pas le disque du système
  CLE=$p; break
done
DEST=/var/backups/sublime-synchro
MONTE=""
if [ -n "$CLE" ]; then
  mkdir -p "$POINT"
  if ! mountpoint -q "$POINT"; then mount "$CLE" "$POINT" && MONTE=1; fi
  mountpoint -q "$POINT" && DEST="$POINT/sauvegardes-sublime"
fi
mkdir -p "$DEST"
F="$DEST/sublime-$(date +%Y-%m-%d_%H%M).tar.gz"
tar -czf "$F.tmp" -C "$(dirname $SRC)" "$(basename $SRC)"
mv "$F.tmp" "$F"
ls -1t "$DEST"/sublime-*.tar.gz | tail -n +31 | xargs -r rm -f
sync
echo "Sauvegarde : $F ($(du -h "$F" | cut -f1))"
[ -n "$MONTE" ] && umount "$POINT" || true
