#!/bin/bash
set -e

echo "Installation NetBox F5 BIG-IP v3.1.0..."

# Arrêter NetBox
sudo systemctl stop netbox netbox-rq

# Backup
if [ -d "/opt/netbox/netbox/netbox_plugins/netbox_f5_bigip" ]; then
    sudo mv /opt/netbox/netbox/netbox_plugins/netbox_f5_bigip \
        /tmp/netbox_f5_bigip_backup_$(date +%Y%m%d_%H%M%S)
fi

# Supprimer anciens
sudo rm -rf /opt/netbox/netbox/netbox_plugins/netbox_f5_*

# Nettoyer caches
sudo find /opt/netbox -name "*.pyc" -delete 2>/dev/null || true
sudo find /opt/netbox -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Installer
sudo cp -r netbox_f5_bigip /opt/netbox/netbox/netbox_plugins/
sudo chown -R netbox:netbox /opt/netbox/netbox/netbox_plugins/netbox_f5_bigip

# Copier script custom fields
sudo cp create_custom_fields.py /opt/netbox/netbox/
sudo chown netbox:netbox /opt/netbox/netbox/create_custom_fields.py

# Config
if ! grep -q "netbox_f5_bigip" /opt/netbox/netbox/netbox/configuration.py; then
    echo ""
    echo "⚠️  Ajoutez dans configuration.py:"
    echo "PLUGINS = ['netbox_f5_bigip']"
    echo ""
fi

# Redémarrer
sudo systemctl start netbox
sleep 5
sudo systemctl start netbox-rq

echo "✅ Installation terminée"
echo ""
echo "Prochaines étapes:"
echo "1. cd /opt/netbox/netbox"
echo "2. sudo -u netbox /opt/netbox/venv/bin/python3 create_custom_fields.py"
echo "3. Videz le cache: Ctrl+Shift+R"
