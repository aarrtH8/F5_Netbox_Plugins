"""Navigation du plugin F5 BIG-IP."""
from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link='plugins:netbox_f5_bigip:home',
        link_text='F5 BIG-IP'
    ),
)
