# Sublime Toit – synchro (serveur du Raspberry Pi)

Synchronise la plateforme et sa carte de prospection entre les téléphones de l'équipe.
Aucun secret dans ce dossier : les comptes (codes hachés) et les données restent sur le Pi,
dans `/var/lib/sublime-synchro`.

## Installer ou mettre à jour

Sur le Pi (Raspberry Pi OS, Tailscale connecté, MagicDNS + HTTPS activés dans la console Tailscale) :

    curl -fsSL https://<site-github>/serveur/installer.sh | sudo bash -s https://<site-github>

Relancer la même commande met le serveur à jour sans toucher aux données ni aux comptes.

## Comptes

    sudo sublime-comptes ajouter yohan "Yohan Changuion" commercial
    sudo sublime-comptes ajouter david "David Debouzy" direction
    sudo sublime-comptes liste
    sudo sublime-comptes code yohan          # changer un code
    sudo sublime-comptes desactiver yohan    # téléphone perdu

## Vérifier

    systemctl status sublime-synchro         # le service tourne ?
    sudo journalctl -u sublime-synchro -n 50 # derniers échanges
    tailscale serve status                   # adresse https publiée
    sudo /opt/sublime-synchro/sauvegarde.sh  # sauvegarde manuelle

Sauvegarde automatique chaque nuit à 2 h 30 : sur la clé USB branchée au Pi
(dossier `sauvegardes-sublime`), sinon dans `/var/backups/sublime-synchro`. 30 archives gardées.

## Déménager vers un autre Pi (Pi 5)

1. Installer le nouveau Pi avec la même commande.
2. Copier `/var/lib/sublime-synchro` de l'ancien vers le nouveau (ou restaurer la dernière archive), puis `sudo chown -R sublime:sublime /var/lib/sublime-synchro`.
3. Dans la console Tailscale : supprimer l'ancien Pi, renommer le nouveau `sublime-pi`. Rien à changer sur les téléphones.
4. Effacer la carte SD de l'ancien Pi.
