# NetBox F5 BIG-IP v3.1.0 - Version LDB

Version simplifiée pour les équipements LDB avec interface unique.

## Fonctionnalités

- Filtre automatique sur les équipements contenant "LDB"
- Récupération automatique de l'IP depuis NetBox
- Interface simple: Device + Credentials → Import
- Mise à jour automatique de l'inventaire

## Installation

```bash
cd /tmp
unzip netbox_f5_bigip_v3.1.0.zip
cd netbox_f5_bigip_v3.1.0
sudo ./install.sh
```

## Configuration

```python
PLUGINS = ['netbox_f5_bigip']
```

## Custom Fields

```bash
cd /opt/netbox/netbox
sudo -u netbox /opt/netbox/venv/bin/python3 create_custom_fields.py
```
