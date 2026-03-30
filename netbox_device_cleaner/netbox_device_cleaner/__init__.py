"""NetBox Device Cleaner — purge complète des objets liés à un équipement."""
from netbox.plugins import PluginConfig


class NetBoxDeviceCleanerConfig(PluginConfig):
    name         = 'netbox_device_cleaner'
    verbose_name = 'Device Cleaner'
    description  = 'Purge complète des objets NetBox liés à un équipement (IPs, VLANs, interfaces, services)'
    version      = '1.0.0'
    author       = 'Squad LAN DC'
    base_url     = 'device-cleaner'
    min_version  = '4.0.0'
    max_version  = '4.9.99'


config = NetBoxDeviceCleanerConfig
