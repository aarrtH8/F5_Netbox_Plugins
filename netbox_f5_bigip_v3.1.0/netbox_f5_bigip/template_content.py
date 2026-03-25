"""Extension de template NetBox — panel F5 Pool sur la page Service."""
import json
from netbox.plugins import PluginTemplateExtension


class ServiceF5PoolPanel(PluginTemplateExtension):
    """
    Ajoute un panneau "F5 — Pool & Membres" sur la page de détail d'un Service.
    N'apparaît que si le service provient d'un import F5 (custom field f5_pool_name renseigné).
    """
    models = ['ipam.service', 'dcim.device', 'virtualization.virtualmachine']

    def right_page(self):
        service = self.context['object']
        cf = service.custom_field_data if isinstance(service.custom_field_data, dict) else {}

        pool_name    = cf.get('f5_pool_name', '')
        members_raw  = cf.get('f5_pool_members', '')
        vip          = cf.get('f5_vip', '')
        destination  = cf.get('f5_destination', '')
        partition    = cf.get('f5_partition', 'Common')
        profiles     = cf.get('f5_profiles', '')
        irules       = cf.get('f5_irules', '')
        snat         = cf.get('f5_snat', '')

        if not pool_name and not vip:
            return ''   # Pas un service F5 — ne rien afficher

        # Parser les membres (JSON depuis importer v3.5.5+, ou ancien format CSV)
        members = []
        if members_raw:
            try:
                members = json.loads(members_raw)
            except (json.JSONDecodeError, TypeError):
                # Ancien format : "1.2.3.4:80, 1.2.3.5:80"
                for m in str(members_raw).split(','):
                    m = m.strip()
                    if ':' in m:
                        addr, port = m.rsplit(':', 1)
                        members.append({'address': addr, 'port': port, 'state': '', 'session': ''})
                    elif m:
                        members.append({'address': m, 'port': '', 'state': '', 'session': ''})

        return self.render('netbox_f5_bigip/service_f5_panel.html', extra_context={
            'pool_name':   pool_name,
            'members':     members,
            'vip':         vip,
            'destination': destination,
            'partition':   partition,
            'profiles':    [p.strip() for p in profiles.split(',') if p.strip()],
            'irules':      [r.strip() for r in irules.split(',') if r.strip()],
            'snat':        snat,
        })


template_extensions = [ServiceF5PoolPanel]
