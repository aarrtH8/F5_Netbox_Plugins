"""NetBox Device Cleaner — vues purge."""
import logging
from django.shortcuts import render
from django.views.generic import View
from django.http import JsonResponse
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from dcim.models import Device, Interface
from virtualization.models import VirtualMachine, VMInterface
from ipam.models import Service, IPAddress, VLAN

logger = logging.getLogger('netbox.plugins.netbox_device_cleaner')


def _purge_object(kind, pk, delete_interfaces):
    """
    Supprime tous les objets NetBox liés à un device ou une VM.

    Supprime toujours :
      - Services (GenericFK)
      - IPAddresses assignées aux interfaces de l'équipement
      - VLANs utilisés EXCLUSIVEMENT par cet équipement (partagés = ignorés)

    Supprime si delete_interfaces=True :
      - Interfaces / VMInterfaces
    """
    if kind == 'vm':
        obj        = VirtualMachine.objects.get(pk=pk)
        obj_ct     = ContentType.objects.get_for_model(VirtualMachine)
        iface_ct   = ContentType.objects.get_for_model(VMInterface)
        ifaces_qs  = VMInterface.objects.filter(virtual_machine=obj)
    else:
        obj        = Device.objects.get(pk=pk)
        obj_ct     = ContentType.objects.get_for_model(Device)
        iface_ct   = ContentType.objects.get_for_model(Interface)
        ifaces_qs  = Interface.objects.filter(device=obj)

    result = {
        'name': obj.name,
        'kind': kind,
        'services': 0,
        'ips': 0,
        'vlans': 0,
        'vlans_skipped': 0,
        'interfaces': 0,
    }

    # ── 1. Services ──────────────────────────────────────────────────
    svc_qs = Service.objects.filter(parent_object_type=obj_ct, parent_object_id=pk)
    result['services'] = svc_qs.count()
    svc_qs.delete()

    # ── 2. IPs assignées aux interfaces ──────────────────────────────
    iface_ids = list(ifaces_qs.values_list('id', flat=True))
    if iface_ids:
        ip_qs = IPAddress.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id__in=iface_ids,
        )
        result['ips'] = ip_qs.count()
        ip_qs.delete()

    # ── 3. VLANs exclusifs ──────────────────────────────────────────
    if iface_ids:
        tagged_ids   = set(ifaces_qs.values_list('tagged_vlans', flat=True)) - {None}
        untagged_ids = set(
            ifaces_qs.exclude(untagged_vlan=None).values_list('untagged_vlan_id', flat=True)
        )
        all_vlan_ids = tagged_ids | untagged_ids

        for vlan_id in all_vlan_ids:
            if kind == 'vm':
                other = (
                    Interface.objects.filter(tagged_vlans=vlan_id).exists() or
                    Interface.objects.filter(untagged_vlan_id=vlan_id).exists() or
                    VMInterface.objects.filter(tagged_vlans=vlan_id).exclude(virtual_machine=obj).exists() or
                    VMInterface.objects.filter(untagged_vlan_id=vlan_id).exclude(virtual_machine=obj).exists()
                )
            else:
                other = (
                    Interface.objects.filter(tagged_vlans=vlan_id).exclude(device=obj).exists() or
                    Interface.objects.filter(untagged_vlan_id=vlan_id).exclude(device=obj).exists() or
                    VMInterface.objects.filter(tagged_vlans=vlan_id).exists() or
                    VMInterface.objects.filter(untagged_vlan_id=vlan_id).exists()
                )

            if other:
                result['vlans_skipped'] += 1
            else:
                VLAN.objects.filter(pk=vlan_id).delete()
                result['vlans'] += 1

    # ── 4. Interfaces (optionnel) ────────────────────────────────────
    if delete_interfaces and iface_ids:
        result['interfaces'] = ifaces_qs.count()
        ifaces_qs.delete()

    logger.info(
        f'[Cleaner] {obj.name}: {result["services"]} services, '
        f'{result["ips"]} IPs, {result["vlans"]} VLANs supprimés '
        f'({result["vlans_skipped"]} VLANs partagés ignorés), '
        f'{result["interfaces"]} interfaces'
    )
    return result


# ─────────────────────────────────────────────────────────────────────────── #
#  Vue principale                                                              #
# ─────────────────────────────────────────────────────────────────────────── #

class PurgeView(View):

    def get(self, request):
        devices = (
            Device.objects
            .select_related('site', 'role', 'device_type', 'tenant')
            .order_by('name')
        )
        vms = (
            VirtualMachine.objects
            .select_related('site', 'role', 'tenant')
            .order_by('name')
        )
        return render(request, 'netbox_device_cleaner/purge.html', {
            'devices': devices,
            'vms': vms,
        })

    def post(self, request):
        items             = request.POST.getlist('items')
        delete_interfaces = request.POST.get('delete_interfaces') == '1'

        if not items:
            return JsonResponse({'success': False, 'error': 'Aucun équipement sélectionné.'})

        results = []
        errors  = []

        try:
            with transaction.atomic():
                for item in items:
                    try:
                        kind, pk = item.split('_', 1)
                        pk = int(pk)
                        if kind not in ('device', 'vm'):
                            continue
                        results.append(_purge_object(kind, pk, delete_interfaces))
                    except Exception as e:
                        errors.append(f'{item}: {e}')
                        logger.error(f'[Cleaner] {item}: {e}')
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

        return JsonResponse({'success': True, 'results': results, 'errors': errors})
