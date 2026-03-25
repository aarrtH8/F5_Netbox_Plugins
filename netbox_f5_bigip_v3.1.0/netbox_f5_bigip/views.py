"""Vues F5 BIG-IP — Home / Connect / Preview / Import."""
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import View
from django.http import JsonResponse
from django.contrib import messages
from django.db.models import Q
from dcim.models import Device
from virtualization.models import VirtualMachine

logger = logging.getLogger('netbox.plugins.netbox_f5_bigip')

SESSION_KEY = 'f5_inventory_{device_id}'
VALID_KINDS = frozenset({'device', 'vm'})


def _get_object(kind, pk):
    """Retourne (objet, kind) selon kind='device' ou kind='vm'."""
    if kind == 'vm':
        return get_object_or_404(VirtualMachine, pk=pk), 'vm'
    return get_object_or_404(Device, pk=pk), 'device'


def _object_ip(obj):
    if obj.primary_ip4:
        return str(obj.primary_ip4.address).split('/')[0]
    if obj.primary_ip6:
        return str(obj.primary_ip6.address).split('/')[0]
    return None


def _object_type_label(obj, kind):
    if kind == 'vm':
        return obj.platform.name if obj.platform else 'Virtual Machine'
    return obj.device_type.model if obj.device_type else ''


# ─────────────────────────────────────────────────────────────────────────── #
#  1. Home — tous les équipements avec le rôle "balancer"                     #
# ─────────────────────────────────────────────────────────────────────────── #

class HomeView(View):

    def get(self, request):
        entries = []

        # ── Devices physiques avec rôle balancer ──
        for d in Device.objects.filter(
            role__name__icontains='balancer'
        ).select_related('device_type', 'primary_ip4', 'primary_ip6', 'role').order_by('name'):
            ip = _object_ip(d)
            cf = d.custom_field_data if isinstance(d.custom_field_data, dict) else {}
            entries.append({
                'id':          d.pk,
                'kind':        'device',
                'name':        d.name,
                'ip':          ip or '',
                'has_ip':      ip is not None,
                'type_label':  _object_type_label(d, 'device'),
                'role':        d.role.name if d.role else '',
                'vs_count':    cf.get('f5_vs_count'),
                'last_sync':   (cf.get('f5_last_sync') or '')[:16].replace('T', ' ') or None,
            })

        # ── Machines virtuelles avec rôle balancer ──
        for vm in VirtualMachine.objects.filter(
            role__name__icontains='balancer'
        ).select_related('platform', 'primary_ip4', 'primary_ip6', 'role').order_by('name'):
            ip = _object_ip(vm)
            cf = vm.custom_field_data if isinstance(vm.custom_field_data, dict) else {}
            entries.append({
                'id':          vm.pk,
                'kind':        'vm',
                'name':        vm.name,
                'ip':          ip or '',
                'has_ip':      ip is not None,
                'type_label':  _object_type_label(vm, 'vm'),
                'role':        vm.role.name if vm.role else '',
                'vs_count':    cf.get('f5_vs_count'),
                'last_sync':   (cf.get('f5_last_sync') or '')[:16].replace('T', ' ') or None,
            })

        entries.sort(key=lambda x: x['name'])
        return render(request, 'netbox_f5_bigip/home.html', {'devices': entries})


# ─────────────────────────────────────────────────────────────────────────── #
#  2. Connect — formulaire credentials                                         #
# ─────────────────────────────────────────────────────────────────────────── #

class ConnectView(View):

    def _get_device_obj(self, kind, device_id):
        if kind == 'vm':
            return get_object_or_404(VirtualMachine, pk=device_id)
        return get_object_or_404(Device, pk=device_id)

    def get(self, request, kind, device_id):
        if kind not in VALID_KINDS:
            messages.error(request, f'Type invalide : {kind}.')
            return redirect('plugins:netbox_f5_bigip:home')
        obj = self._get_device_obj(kind, device_id)
        ip  = _object_ip(obj)
        if not ip:
            messages.error(request, f'Aucune IP configurée pour {obj.name}.')
            return redirect('plugins:netbox_f5_bigip:home')
        return render(request, 'netbox_f5_bigip/connect.html', {
            'device':      obj,
            'kind':        kind,
            'f5_ip':       ip,
            'type_label':  _object_type_label(obj, kind),
        })

    def post(self, request, kind, device_id):
        if kind not in VALID_KINDS:
            messages.error(request, f'Type invalide : {kind}.')
            return redirect('plugins:netbox_f5_bigip:home')
        obj  = self._get_device_obj(kind, device_id)
        ip   = _object_ip(obj)
        if not ip:
            messages.error(request, f'Aucune IP pour {obj.name}.')
            return redirect('plugins:netbox_f5_bigip:home')

        f5_user     = request.POST.get('f5_user', '').strip()
        f5_password = request.POST.get('f5_password', '').strip()
        if not f5_user or not f5_password:
            messages.error(request, 'Identifiants manquants.')
            return redirect('plugins:netbox_f5_bigip:connect', kind=kind, device_id=device_id)

        try:
            from netbox_f5_bigip.services.f5_client import F5Client
            client    = F5Client(host=ip, username=f5_user, password=f5_password)
            inventory = client.fetch_full_inventory()

            key = SESSION_KEY.format(device_id=f'{kind}_{device_id}')
            request.session[key] = {'inventory': inventory, 'host': ip}
            return redirect('plugins:netbox_f5_bigip:preview', kind=kind, device_id=device_id)

        except Exception as e:
            logger.error(f'Connexion F5 {ip} : {e}')
            messages.error(request, f'Connexion échouée : {e}')
            return redirect('plugins:netbox_f5_bigip:connect', kind=kind, device_id=device_id)


# ─────────────────────────────────────────────────────────────────────────── #
#  3. Preview — visualisation DataTables                                       #
# ─────────────────────────────────────────────────────────────────────────── #

class PreviewView(View):

    def _get_device_obj(self, kind, device_id):
        if kind == 'vm':
            return get_object_or_404(VirtualMachine, pk=device_id)
        return get_object_or_404(Device, pk=device_id)

    def get(self, request, kind, device_id):
        obj = self._get_device_obj(kind, device_id)
        key = SESSION_KEY.format(device_id=f'{kind}_{device_id}')
        data = request.session.get(key)
        if not data:
            messages.error(request, 'Session expirée. Reconnectez-vous.')
            return redirect('plugins:netbox_f5_bigip:connect', kind=kind, device_id=device_id)

        inv = data['inventory']

        vs_list = []
        for vs in inv.get('virtual_servers', []):
            if not isinstance(vs, dict):
                continue
            dest = vs.get('destination', '')
            
            # Sécuriser profiles
            profiles = vs.get('profiles', [])
            if isinstance(profiles, list):
                profiles_str = ', '.join([str(p) for p in profiles if p])
            else:
                profiles_str = ''
            
            # Sécuriser rules
            rules = vs.get('rules', [])
            if isinstance(rules, list):
                rules_str = ', '.join([str(r) for r in rules if r])
            else:
                rules_str = ''
            
            vs_list.append({
                'name':      vs.get('name', ''),
                'partition': vs.get('partition', 'Common'),
                'dest':      dest,
                'protocol':  vs.get('ipProtocol', 'tcp'),
                'pool':      vs.get('pool', '').split('/')[-1],
                'profiles':  profiles_str,
                'rules':     rules_str,
                'enabled':   vs.get('enabled', True),
            })

        pool_list = []
        for p in inv.get('pools', []):
            if not isinstance(p, dict):
                continue
            members = p.get('members_list', [])
            
            # Sécuriser members pour l'affichage
            members_display = []
            if isinstance(members, list):
                for m in members:
                    if isinstance(m, dict):
                        addr = m.get('address', '')
                        port = m.get('port', '')
                        if addr:
                            members_display.append(f"{addr}:{port}")
            
            pool_list.append({
                'name':      p.get('name', ''),
                'partition': p.get('partition', 'Common'),
                'lb_method': p.get('loadBalancingMode', ''),
                'monitor':   p.get('monitor', ''),
                'nb':        len(members) if isinstance(members, list) else 0,
                'members':   ', '.join(members_display),
            })

        node_list = []
        for n in inv.get('nodes', []):
            if not isinstance(n, dict):
                continue
            state = n.get('state', '')
            node_list.append({
                'name':      n.get('name', ''),
                'address':   n.get('address', ''),
                'partition': n.get('partition', 'Common'),
                'state':     state,
                'state_cls': 'success' if state == 'enabled' else 'danger' if state == 'disabled' else 'secondary',
            })

        vlan_list = []
        for v in inv.get('vlans', []):
            if not isinstance(v, dict):
                continue
            vlan_list.append({
                'name':      v.get('name', ''),
                'tag':       v.get('tag', ''),
                'mtu':       v.get('mtu', ''),
                'partition': v.get('partition', 'Common'),
            })

        selfip_list = []
        for s in inv.get('self_ips', []):
            if not isinstance(s, dict):
                continue
            selfip_list.append({
                'name':          s.get('name', ''),
                'address':       s.get('address', ''),
                'vlan':          s.get('vlan', '').split('/')[-1],
                'traffic_group': s.get('trafficGroup', '').split('/')[-1],
                'partition':     s.get('partition', 'Common'),
            })

        raw_counts = {k: len(v) if isinstance(v, list) else type(v).__name__
                      for k, v in inv.items()}

        return render(request, 'netbox_f5_bigip/preview.html', {
            'device':      obj,
            'kind':        kind,
            'f5_host':     data.get('host', ''),
            'vs_list':     vs_list,
            'pool_list':   pool_list,
            'node_list':   node_list,
            'vlan_list':   vlan_list,
            'selfip_list': selfip_list,
            'raw_counts':  raw_counts,
        })


# ─────────────────────────────────────────────────────────────────────────── #
#  4. Import — lance la tâche RQ en arrière-plan                              #
# ─────────────────────────────────────────────────────────────────────────── #

class ImportView(View):
    """Lance le job RQ et retourne immédiatement le job_id."""

    def post(self, request, kind, device_id):
        key  = SESSION_KEY.format(device_id=f'{kind}_{device_id}')
        data = request.session.get(key)
        if not data:
            return JsonResponse({'success': False, 'error': 'Session expirée.'})

        try:
            from netbox_f5_bigip.jobs import run_f5_import
            rq_job = run_f5_import.delay(
                device_id=int(device_id),
                kind=kind,
                inventory=data['inventory'],
            )
            # Conserver la session jusqu'à la fin du job
            return JsonResponse({'success': True, 'job_id': rq_job.id})
        except Exception as e:
            logger.error(f'Import {kind}/{device_id} : {e}')
            return JsonResponse({'success': False, 'error': str(e)})


# ─────────────────────────────────────────────────────────────────────────── #
#  5. JobStatus — polling du statut du job RQ                                 #
# ─────────────────────────────────────────────────────────────────────────── #

class JobStatusView(View):
    """Endpoint de polling : retourne le statut et le résultat du job RQ."""

    def get(self, request, job_id):
        try:
            import django_rq
            from rq.job import Job

            queue  = django_rq.get_queue('default')
            rq_job = Job.fetch(job_id, connection=queue.connection)

            # get_status() retourne un enum dans RQ >= 1.16 → forcer en string
            status_raw = rq_job.get_status()
            status = status_raw.value if hasattr(status_raw, 'value') else str(status_raw)

            if status == 'finished':
                # RQ < 1.16 : job.result est le dict retourné directement
                # RQ >= 1.16 : job.result est un objet Result → .return_value
                raw = rq_job.result
                logger.debug(f'[F5 Job] result type={type(raw).__name__} value={repr(raw)[:200]}')
                if hasattr(raw, 'return_value'):
                    stats = raw.return_value or {}
                elif callable(getattr(rq_job, 'return_value', None)):
                    stats = rq_job.return_value() or {}
                elif isinstance(raw, dict):
                    stats = raw
                else:
                    # Dernier recours : latest_result().return_value (RQ 1.16+)
                    try:
                        stats = rq_job.latest_result().return_value or {}
                    except Exception:
                        stats = {}

                # S'assurer que stats contient bien les clés attendues
                expected = ['virtual_servers', 'pools', 'nodes', 'vlans', 'self_ips', 'errors']
                if not any(k in stats for k in expected):
                    # Peut-être que stats est wrappé dans un niveau supplémentaire
                    stats = {}

                return JsonResponse({'status': 'finished', 'stats': stats})

            elif status == 'failed':
                exc = str(rq_job.exc_info or '')
                last_line = [l for l in exc.splitlines() if l.strip()][-1] if exc else 'Erreur inconnue'
                return JsonResponse({'status': 'failed', 'error': last_line})

            else:
                return JsonResponse({'status': status})

        except Exception as e:
            logger.error(f'JobStatus {job_id} : {e}')
            return JsonResponse({'status': 'error', 'error': str(e)})
