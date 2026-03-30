from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link='plugins:netbox_device_cleaner:purge',
        link_text='Device Cleaner',
    ),
)
