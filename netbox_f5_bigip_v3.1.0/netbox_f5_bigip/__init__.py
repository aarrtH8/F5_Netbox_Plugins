"""NetBox F5 BIG-IP Plugin."""
from netbox.plugins import PluginConfig


class NetBoxF5BigIPConfig(PluginConfig):

    name         = 'netbox_f5_bigip'
    verbose_name = 'F5 BIG-IP'
    description  = 'Import et gestion F5 BIG-IP'
    version      = '3.10.5'
    author       = 'Squad LAN DC'
    base_url     = 'f5bigip'
    min_version  = '4.0.0'
    max_version  = '4.9.99'

    default_settings = {
        'fetch_timeout': 120,
        'api_timeout':   30,
    }

    def ready(self):
        super().ready()
        try:
            _ensure_custom_fields()
        except Exception as e:
            import logging
            logging.getLogger('netbox.plugins.netbox_f5_bigip').warning(
                f'[F5] ready() custom fields : {e}'
            )


def _ensure_custom_fields():
    """
    Crée ou met à jour les custom fields F5.
    Compatible NetBox 4.x (object_types) et 3.x (content_types).
    Idempotent — peut être appelé plusieurs fois.
    """
    from django.contrib.contenttypes.models import ContentType
    from extras.models import CustomField
    from dcim.models import Device
    from virtualization.models import VirtualMachine
    from ipam.models import Service

    import logging
    log = logging.getLogger('netbox.plugins.netbox_f5_bigip')

    ct_device  = ContentType.objects.get_for_model(Device)
    ct_vm      = ContentType.objects.get_for_model(VirtualMachine)
    ct_service = ContentType.objects.get_for_model(Service)

    # Détecter si on utilise object_types (NetBox 4.x) ou content_types (NetBox 3.x)
    sample = CustomField()
    if hasattr(sample, 'object_types'):
        CT_ATTR = 'object_types'
    elif hasattr(sample, 'content_types'):
        CT_ATTR = 'content_types'
    else:
        log.warning('[F5] Impossible de déterminer l\'attribut content_types/object_types')
        return

    # (field_name, label, type, [content_types])
    FIELDS = [
        # ── Sur Device ET VirtualMachine ────────────────────────────────
        ('f5_vs_count',     'F5 — Nb Virtual Servers', 'integer', [ct_device, ct_vm]),
        ('f5_pool_count',   'F5 — Nb Pools',           'integer', [ct_device, ct_vm]),
        ('f5_node_count',   'F5 — Nb Nodes',           'integer', [ct_device, ct_vm]),
        ('f5_vlan_count',   'F5 — Nb VLANs',           'integer', [ct_device, ct_vm]),
        ('f5_selfip_count', 'F5 — Nb Self IPs',        'integer', [ct_device, ct_vm]),
        ('f5_last_sync',    'F5 — Dernière synchro',   'datetime', [ct_device, ct_vm]),

        # ── Sur Service (Virtual Servers importés) ───────────────────────
        ('f5_destination',  'F5 — Destination',   'text',     [ct_service]),
        ('f5_vip',          'F5 — VIP',           'text',     [ct_service]),
        ('f5_pool_name',    'F5 — Pool',          'text',     [ct_service]),
        ('f5_pool_members', 'F5 — Pool Members',  'longtext', [ct_service]),
        ('f5_profiles',     'F5 — Profiles',      'text',     [ct_service]),
        ('f5_irules',       'F5 — iRules',        'text',     [ct_service]),
        ('f5_snat',         'F5 — SNAT',          'text',     [ct_service]),
        ('f5_partition',    'F5 — Partition',     'text',     [ct_service]),
        ('f5_device_id',    'F5 — Device ID',     'integer',  [ct_service]),
    ]

    created_count = 0
    updated_count = 0

    for name, label, ftype, expected_cts in FIELDS:
        try:
            cf, created = CustomField.objects.get_or_create(
                name=name,
                defaults={'label': label, 'type': ftype, 'required': False}
            )

            # Vérifier et corriger les object types
            ct_manager = getattr(cf, CT_ATTR)
            current_cts = set(ct_manager.all())
            expected_set = set(expected_cts)

            if current_cts != expected_set:
                # Utiliser .set() pour remplacer complètement (pas .add())
                ct_manager.set(expected_cts)
                updated_count += 1
                log.info(f'[F5] Custom field {name} : object types mis à jour')

            if created:
                created_count += 1
                log.info(f'[F5] Custom field créé : {name}')

        except Exception as e:
            log.warning(f'[F5] Custom field {name} : {e}')

    if created_count:
        log.info(f'[F5] {created_count} custom field(s) créé(s)')
    if updated_count:
        log.info(f'[F5] {updated_count} custom field(s) mis à jour (object types corrigés)')


config = NetBoxF5BigIPConfig
