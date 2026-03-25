#!/usr/bin/env python3
"""Crée les custom fields nécessaires au plugin F5 BIG-IP v3.1.2."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')

import django
django.setup()

from django.contrib.contenttypes.models import ContentType
from extras.models import CustomField

# Content types
from dcim.models import Device
from ipam.models import Service

ct_device  = ContentType.objects.get_for_model(Device)
ct_service = ContentType.objects.get_for_model(Service)

# -------------------------------------------------------------------------- #
# Champs sur le Device (résumé F5)
# -------------------------------------------------------------------------- #
DEVICE_FIELDS = [
    dict(name='f5_vs_count',     label='F5 VS Count',        type='integer'),
    dict(name='f5_pool_count',   label='F5 Pool Count',       type='integer'),
    dict(name='f5_node_count',   label='F5 Node Count',       type='integer'),
    dict(name='f5_vlan_count',   label='F5 VLAN Count',       type='integer'),
    dict(name='f5_selfip_count', label='F5 Self IP Count',    type='integer'),
    dict(name='f5_last_sync',    label='F5 Last Sync',        type='text'),
]

# -------------------------------------------------------------------------- #
# Champs sur les Services (Virtual Servers)
# -------------------------------------------------------------------------- #
SERVICE_FIELDS = [
    dict(name='f5_destination',  label='F5 Destination',      type='text'),
    dict(name='f5_pool_name',    label='F5 Pool Name',         type='text'),
    dict(name='f5_pool_members', label='F5 Pool Members',      type='longtext'),
    dict(name='f5_profiles',     label='F5 Profiles',          type='text'),
    dict(name='f5_irules',       label='F5 iRules',            type='text'),
    dict(name='f5_snat',         label='F5 SNAT',              type='text'),
    dict(name='f5_partition',    label='F5 Partition',         type='text'),
    dict(name='f5_device_id',    label='F5 Device ID',         type='integer'),
]

created = 0
updated = 0

def ensure_field(field_def, content_types):
    global created, updated
    name = field_def['name']
    cf, was_created = CustomField.objects.get_or_create(
        name=name,
        defaults={
            'label':    field_def['label'],
            'type':     field_def['type'],
            'required': False,
        }
    )
    for ct in content_types:
        cf.content_types.add(ct)
    if was_created:
        print(f"  ✅ Créé  : {name}")
        created += 1
    else:
        print(f"  ↩  Existe : {name}")
        updated += 1

print("\nChamps sur Device :")
for f in DEVICE_FIELDS:
    ensure_field(f, [ct_device])

print("\nChamps sur Service (Virtual Servers) :")
for f in SERVICE_FIELDS:
    ensure_field(f, [ct_service])

print(f"\nTerminé — {created} créés, {updated} déjà existants")
