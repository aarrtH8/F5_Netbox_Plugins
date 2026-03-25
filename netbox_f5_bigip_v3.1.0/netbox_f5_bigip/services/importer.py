"""Importer F5 → NetBox avec synchronisation bidirectionnelle."""
import logging
import traceback
import json
import re
from typing import Dict, Any, Optional, Set
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from dcim.models import Device, Interface, Site
from virtualization.models import VirtualMachine, VMInterface
from ipam.models import Service, IPAddress, VLAN, VLANGroup, Prefix

logger = logging.getLogger('netbox.plugins.netbox_f5_bigip')

PROTOCOL_MAP = {'tcp': 'tcp', 'udp': 'udp', 'sctp': 'sctp', 'any': 'tcp'}


class F5Importer:

    def __init__(self, device_id: int, kind: str = 'device'):
        self.device_id = int(device_id)
        self.kind      = kind
        if kind == 'vm':
            self.device = VirtualMachine.objects.get(pk=self.device_id)
        else:
            self.device = Device.objects.get(pk=self.device_id)

        self.stats = {
            'virtual_servers': 0,
            'pools':           0,
            'nodes':           0,
            'vlans':           0,
            'self_ips':        0,
            'errors':          0,
            'deleted':         0,
        }
        self._ip_cache: Dict[str, int] = {}

        # Tracking pour synchronisation
        self._imported_service_ids: Set[int] = set()
        self._imported_ip_ids: Set[int] = set()
        self._imported_vlan_ids: Set[int] = set()

        # Mapping interfaces F5 → NetBox et trunks
        self._interface_map: Dict[str, Any] = {}
        self._trunk_members: Dict[str, list] = {}
        self._vlan_cache: Dict[int, VLAN] = {}

        # VLANGroup partagé pour le site du device (créé au premier besoin)
        self._vlan_group: Optional[VLANGroup] = None

    # ── Utilitaires ─────────────────────────────────────────────────────── #

    def _get_device_site(self) -> Optional[Site]:
        """Retourne le site du device (Device ou VirtualMachine via cluster)."""
        if self.kind == 'vm':
            # VM peut avoir un site direct (NetBox 4.x) ou via cluster
            site = getattr(self.device, 'site', None)
            if site:
                return site
            cluster = getattr(self.device, 'cluster', None)
            if cluster:
                return getattr(cluster, 'site', None)
            return None
        return getattr(self.device, 'site', None)

    def _get_or_create_vlan_group(self, site: Optional[Site]) -> Optional[VLANGroup]:
        """
        Retourne ou crée un VLANGroup F5 scopé sur le site donné.
        Nommage : "F5 — {site.name}" / slug : "f5-{site.slug}"
        Idempotent : renvoie toujours le même groupe pour le site.
        """
        if not site:
            return None
        if self._vlan_group is not None:
            return self._vlan_group
        try:
            ct_site   = ContentType.objects.get_for_model(Site)
            group_slug = f'f5-{site.slug}'
            group_name = f'F5 — {site.name}'
            group, created = VLANGroup.objects.get_or_create(
                slug=group_slug,
                defaults={
                    'name':       group_name,
                    'scope_type': ct_site,
                    'scope_id':   site.pk,
                }
            )
            if created:
                logger.info(f'[F5] VLANGroup créé : "{group_name}" (site {site.name})')
            self._vlan_group = group
            return group
        except Exception as e:
            logger.warning(f'[F5] VLANGroup {site.name} : {e}')
            return None

    def _extract_ip_port(self, destination: str):
        if not destination:
            return None, None
        dest = destination.split('%')[0]
        if '/' in dest:
            dest = dest.rsplit('/', 1)[-1]
        if ':' in dest:
            ip, port_str = dest.rsplit(':', 1)
            try:
                port = int(port_str)
                return ip, port if 1 <= port <= 65535 else None
            except ValueError:
                return ip, None
        return dest, None

    def _get_or_create_ip(self, address: str, description: str,
                           tenant=None, role: str = '',
                           status: str = 'active') -> Optional[IPAddress]:
        """
        Crée ou récupère une IPAddress NetBox.
        Hérite du tenant du device, applique le rôle et le statut fournis.
        """
        if not address or address in ('any', '0.0.0.0'):
            return None
        addr = address.split('%')[0]
        if '/' not in addr:
            addr += '/32'
        if addr in self._ip_cache:
            return IPAddress.objects.filter(pk=self._ip_cache[addr]).first()
        try:
            defaults: Dict[str, Any] = {
                'description': description,
                'status':      status,
            }
            if role:
                defaults['role'] = role
            if tenant:
                defaults['tenant'] = tenant

            obj, created = IPAddress.objects.get_or_create(
                address=addr,
                defaults=defaults,
            )
            if not created:
                # Mise à jour partielle des champs absents seulement
                updated_fields = []
                if description and not obj.description:
                    obj.description = description
                    updated_fields.append('description')
                if status and obj.status != status:
                    obj.status = status
                    updated_fields.append('status')
                if role and not obj.role:
                    obj.role = role
                    updated_fields.append('role')
                if tenant and not obj.tenant:
                    obj.tenant = tenant
                    updated_fields.append('tenant')
                if updated_fields:
                    obj.save(update_fields=updated_fields)

            self._ip_cache[addr] = obj.pk
            self._imported_ip_ids.add(obj.pk)
            return obj
        except Exception as e:
            logger.error(f'IP {addr} : {e}')
            return None

    def _set_cf(self, obj, updates: dict):
        cf = dict(obj.custom_field_data) if isinstance(obj.custom_field_data, dict) else {}
        cf.update(updates)
        obj.custom_field_data = cf

    def _save_service(self, name: str, vip: str, port: int,
                      protocol: str, description: str) -> Optional[Service]:
        """
        Crée ou met à jour un Service NetBox.
        Nom format : "VS_NAME (IP:PORT)" — tronqué à 100 chars (limite NetBox).
        Utilise le GenericFK parent_object_type/id (NetBox 4.x).
        """
        display_name = f"{name} ({vip}:{port})" if vip else name
        # Limite NetBox : Service.name max_length=100
        display_name = display_name[:100]

        if self.kind == 'vm':
            ct = ContentType.objects.get_for_model(VirtualMachine)
        else:
            ct = ContentType.objects.get_for_model(Device)

        # Chercher par nom VS original, puis nom formaté
        qs = Service.objects.filter(
            name=name,
            parent_object_type=ct,
            parent_object_id=self.device.pk,
        )
        if not qs.exists():
            qs = Service.objects.filter(
                name=display_name,
                parent_object_type=ct,
                parent_object_id=self.device.pk,
            )

        if qs.exists():
            service = qs.first()
            service.name        = display_name
            service.protocol    = protocol
            service.ports       = [port]
            service.description = description
            service.save(update_fields=['name', 'protocol', 'ports', 'description'])
            self._imported_service_ids.add(service.pk)
            return service

        service = Service(
            name=display_name,
            protocol=protocol,
            ports=[port],
            description=description,
            parent_object_type=ct,
            parent_object_id=self.device.pk,
        )
        service.save()
        self._imported_service_ids.add(service.pk)
        return service

    def _link_vip_to_service(self, service: Service, ip_str: str, vs_name: str):
        """Crée l'IPAddress VIP (role=vip) et la lie au Service via M2M ipaddresses."""
        if not ip_str or ip_str in ('any', '0.0.0.0'):
            return
        tenant = getattr(self.device, 'tenant', None)
        vip = self._get_or_create_ip(
            ip_str,
            f'[F5 VIP] {vs_name} ({self.device.name})',
            tenant=tenant,
            role='vip',
        )
        if not vip:
            return
        try:
            service.ipaddresses.add(vip)
        except Exception as e:
            logger.debug(f'ipaddresses.add() : {e}')

    def _associate_vlan_to_prefix(self, vlan: VLAN):
        """
        Associe le VLAN créé à un préfixe existant dans NetBox
        (cherche par correspondance de nom dans le même site).
        """
        try:
            qs = Prefix.objects.filter(vlan__isnull=True)
            if vlan.site:
                qs = qs.filter(site=vlan.site)
            candidates = qs.filter(description__icontains=vlan.name)[:1]
            if candidates:
                prefix = candidates[0]
                prefix.vlan = vlan
                prefix.save(update_fields=['vlan'])
                logger.info(f'[F5] VLAN {vlan.name} associé au préfixe {prefix}')
        except Exception as e:
            logger.debug(f'[F5] Association VLAN→Prefix : {e}')

    def _build_interface_mapping(self, interfaces: list, trunks: list):
        """
        Construit le mapping entre interfaces F5 et interfaces NetBox.
        Gère aussi les trunks/agrégations.
        """
        if isinstance(trunks, list):
            for trunk in trunks:
                if not isinstance(trunk, dict):
                    continue
                trunk_name = trunk.get('name', '').split('/')[-1]
                members    = trunk.get('member_interfaces', [])
                if trunk_name and members:
                    self._trunk_members[trunk_name] = members
                    logger.debug(f'[F5] Trunk {trunk_name} → {members}')

        if self.kind == 'vm':
            netbox_interfaces = VMInterface.objects.filter(virtual_machine=self.device)
        else:
            netbox_interfaces = Interface.objects.filter(device=self.device)

        for iface in netbox_interfaces:
            f5_name = self._normalize_interface_name(iface.name)
            if f5_name:
                self._interface_map[f5_name] = iface
                logger.debug(f'[F5] Mapping {f5_name} (F5) → {iface.name} (NetBox)')

    def _normalize_interface_name(self, name: str) -> str:
        """Convertit un nom d'interface NetBox vers le format F5 (ex: "1.1")."""
        match = re.search(r'(\d+)[./](\d+)', name)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
        match = re.search(r'eth(\d+)', name.lower())
        if match:
            return f"1.{match.group(1)}"
        return name

    def _resolve_f5_interface_to_netbox(self, f5_interface: str) -> list:
        """Résout une interface F5 (ou trunk) vers les interfaces NetBox correspondantes."""
        if f5_interface in self._trunk_members:
            return [
                self._interface_map[m]
                for m in self._trunk_members[f5_interface]
                if m in self._interface_map
            ]
        if f5_interface in self._interface_map:
            return [self._interface_map[f5_interface]]
        return []

    # ── Import principal ─────────────────────────────────────────────────── #

    def import_inventory(self, inventory: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from netbox_f5_bigip import _ensure_custom_fields
            _ensure_custom_fields()
        except Exception as e:
            logger.warning(f'Custom fields : {e}')

        logger.info(f'[F5] Import → {self.device.name}')

        # 1. Mapping interfaces / trunks
        self._build_interface_mapping(
            inventory.get('interfaces', []),
            inventory.get('trunks', []),
        )

        # 2. Nodes, VLANs, Self IPs
        for node in inventory.get('nodes', []):
            if isinstance(node, dict):
                self._import_node(node)

        for vlan in inventory.get('vlans', []):
            if isinstance(vlan, dict):
                self._import_vlan(vlan)

        for selfip in inventory.get('self_ips', []):
            if isinstance(selfip, dict):
                self._import_self_ip(selfip)

        # 3. Pools + Virtual Servers
        pools_by_name: Dict[str, dict] = {}
        for pool in inventory.get('pools', []):
            if isinstance(pool, dict):
                name = pool.get('name', '')
                if name:
                    pools_by_name[name] = pool
                    self.stats['pools'] += 1

        for vs in inventory.get('virtual_servers', []):
            if isinstance(vs, dict):
                self._import_vs(vs, pools_by_name)

        # 4. Nettoyage des objets orphelins
        self._cleanup_orphaned_objects()

        self._update_device()
        logger.info(f'[F5] Import terminé : {self.stats}')
        return self.stats

    # ── Objets individuels ───────────────────────────────────────────────── #

    def _import_node(self, data: dict):
        name    = data.get('name', '')
        address = data.get('address', '').split('%')[0]
        if not name or not address or address == 'any':
            return
        try:
            with transaction.atomic():
                tenant = getattr(self.device, 'tenant', None)
                if self._get_or_create_ip(
                    address,
                    f'[F5 Node] {name} ({self.device.name})',
                    tenant=tenant,
                ):
                    self.stats['nodes'] += 1
        except Exception as e:
            logger.error(f'Node {name} : {e}')
            self.stats['errors'] += 1

    def _import_vlan(self, data: dict):
        name              = data.get('name', '')
        tag               = data.get('tag')
        tagged_interfaces = data.get('tagged_interfaces', [])

        if not name:
            return
        try:
            with transaction.atomic():
                vid = int(tag) if tag else None
                if not vid or not (1 <= vid <= 4094):
                    return

                device_tenant = getattr(self.device, 'tenant', None)
                site          = self._get_device_site()
                group         = self._get_or_create_vlan_group(site)

                # Recherche du VLAN existant — ordre de priorité :
                # 1. Même groupe (le plus précis, évite les doublons cross-site)
                # 2. Même tenant + site
                # 3. Même tenant
                # 4. Sans tenant
                # 5. N'importe lequel avec ce VID
                vlan = None

                if group:
                    vlan = VLAN.objects.filter(vid=vid, group=group).first()

                if not vlan and device_tenant and site:
                    vlan = VLAN.objects.filter(vid=vid, tenant=device_tenant, site=site).first()

                if not vlan and device_tenant:
                    vlan = VLAN.objects.filter(vid=vid, tenant=device_tenant).first()

                if not vlan:
                    vlan = VLAN.objects.filter(vid=vid, tenant__isnull=True).first()

                if not vlan:
                    vlan = VLAN.objects.filter(vid=vid).first()

                if vlan:
                    # VLAN existant : enrichir avec groupe/site/tenant si absents
                    updated_fields = []
                    if group and not vlan.group:
                        vlan.group = group
                        updated_fields.append('group')
                    if site and not vlan.site:
                        vlan.site = site
                        updated_fields.append('site')
                    if device_tenant and not vlan.tenant:
                        vlan.tenant = device_tenant
                        updated_fields.append('tenant')
                    if updated_fields:
                        vlan.save(update_fields=updated_fields)
                    logger.info(f'[F5] VLAN {vid} existant réutilisé : "{vlan.name}"'
                                + (f' → enrichi ({", ".join(updated_fields)})' if updated_fields else ''))
                else:
                    # Créer le VLAN avec toutes les métadonnées NetBox
                    vlan = VLAN(
                        vid=vid,
                        name=name,
                        status='active',
                        site=site,
                        group=group,
                        tenant=device_tenant,
                    )
                    vlan.save()
                    logger.info(f'[F5] VLAN créé : {name} (VID {vid}'
                                f', site {site.name if site else "N/A"}'
                                f', groupe {group.name if group else "N/A"})')
                    self._associate_vlan_to_prefix(vlan)

                self._vlan_cache[vid] = vlan
                self._imported_vlan_ids.add(vlan.pk)
                self.stats['vlans'] += 1

                # Associer aux interfaces NetBox taggées
                if isinstance(tagged_interfaces, list):
                    for f5_iface in tagged_interfaces:
                        if not isinstance(f5_iface, str):
                            continue
                        for nb_iface in self._resolve_f5_interface_to_netbox(f5_iface):
                            if hasattr(nb_iface, 'tagged_vlans'):
                                if vlan not in nb_iface.tagged_vlans.all():
                                    nb_iface.tagged_vlans.add(vlan)
                                    logger.info(f'[F5] VLAN {vid} ajouté à {nb_iface.name}')

        except Exception as e:
            logger.error(f'VLAN {name} (VID {tag}) : {e}')
            self.stats['errors'] += 1

    def _import_self_ip(self, data: dict):
        name     = data.get('name', '')
        address  = data.get('address', '').split('%')[0]
        vlan_ref = data.get('vlan', '')  # Ex: "/Common/vlan100"

        if not name or not address:
            return

        try:
            with transaction.atomic():
                tenant    = getattr(self.device, 'tenant', None)
                vlan_name = vlan_ref.split('/')[-1] if vlan_ref else None

                # Trouver le VLAN correspondant dans le cache
                vlan = None
                if vlan_name:
                    for vid, v in self._vlan_cache.items():
                        if vlan_name.lower() in v.name.lower() or str(vid) in vlan_name:
                            vlan = v
                            break

                if '/' not in address:
                    address += '/32'

                # Créer ou récupérer l'IP (statut active, tenant propagé)
                ip_obj = self._get_or_create_ip(
                    address,
                    f'[F5 Self IP] {name} ({self.device.name})',
                    tenant=tenant,
                )
                if not ip_obj:
                    return

                # Associer au VLAN
                if vlan and ip_obj.vlan != vlan:
                    ip_obj.vlan = vlan
                    ip_obj.save(update_fields=['vlan'])
                    logger.info(f'[F5] IP {address} associée au VLAN {vlan.name}')

                # Associer à l'interface NetBox qui porte ce VLAN
                # Utiliser les champs GFK explicites (assigned_object_type / id)
                if vlan:
                    if self.kind == 'vm':
                        candidate_ifaces = VMInterface.objects.filter(
                            virtual_machine=self.device,
                            tagged_vlans=vlan,
                        )
                    else:
                        candidate_ifaces = Interface.objects.filter(
                            device=self.device,
                            tagged_vlans=vlan,
                        )

                    if candidate_ifaces.exists():
                        target_iface = candidate_ifaces.first()
                        ct = ContentType.objects.get_for_model(type(target_iface))
                        if (ip_obj.assigned_object_type != ct
                                or ip_obj.assigned_object_id != target_iface.pk):
                            ip_obj.assigned_object_type = ct
                            ip_obj.assigned_object_id   = target_iface.pk
                            ip_obj.save(update_fields=['assigned_object_type',
                                                       'assigned_object_id'])
                            logger.info(f'[F5] IP {address} assignée à {target_iface.name}')

                self.stats['self_ips'] += 1

        except Exception as e:
            logger.error(f'Self IP {name} : {e}')
            self.stats['errors'] += 1

    def _import_vs(self, data: dict, pools_by_name: dict):
        name = data.get('name', '')
        if not name:
            return

        destination = data.get('destination', '')
        ip_str, port = self._extract_ip_port(destination)
        if not port:
            port = 443

        protocol  = PROTOCOL_MAP.get(data.get('ipProtocol', 'tcp').lower(), 'tcp')
        pool_ref  = data.get('pool', '')
        pool_name = pool_ref.split('/')[-1] if pool_ref else ''
        pool_data = pools_by_name.get(pool_name, {})
        members   = pool_data.get('members_list', [])

        members_clean = []
        if isinstance(members, list):
            for m in members:
                if isinstance(m, dict):
                    members_clean.append({
                        'address': m.get('address', ''),
                        'port':    m.get('port', 0),
                    })
        members_json = json.dumps(members_clean)

        profiles = data.get('profiles', [])
        profiles_str = ', '.join([str(p) for p in profiles if p]) if isinstance(profiles, list) else ''

        rules = data.get('rules', [])
        rules_str = ', '.join([str(r) for r in rules if r]) if isinstance(rules, list) else ''

        snat_data = data.get('sourceAddressTranslation', {})
        snat_type = snat_data.get('type', '') if isinstance(snat_data, dict) else ''

        try:
            with transaction.atomic():
                service = self._save_service(
                    name=name,
                    vip=ip_str or '',
                    port=port,
                    protocol=protocol,
                    description=data.get('description', ''),
                )
                if not service:
                    self.stats['errors'] += 1
                    return

                self._set_cf(service, {
                    'f5_destination':  destination,
                    'f5_vip':          ip_str or '',
                    'f5_pool_name':    pool_name,
                    'f5_pool_members': members_json,
                    'f5_profiles':     profiles_str,
                    'f5_irules':       rules_str,
                    'f5_snat':         snat_type,
                    'f5_partition':    data.get('partition', 'Common'),
                    'f5_device_id':    self.device_id,
                })
                service.save(update_fields=['custom_field_data'])

                self._link_vip_to_service(service, ip_str, name)
                self.stats['virtual_servers'] += 1

        except Exception as e:
            logger.error(f'VS {name} : {e} | {traceback.format_exc().splitlines()[-2]}')
            self.stats['errors'] += 1

    def _cleanup_orphaned_objects(self):
        """
        Supprime les objets NetBox créés par ce plugin qui ne sont plus
        présents dans la config F5.
        Utilise des queries DB pour éviter de charger tous les objets en mémoire.
        """
        try:
            if self.kind == 'vm':
                ct = ContentType.objects.get_for_model(VirtualMachine)
            else:
                ct = ContentType.objects.get_for_model(Device)

            # ── Services (Virtual Servers) ──
            orphaned_services = Service.objects.filter(
                parent_object_type=ct,
                parent_object_id=self.device.pk,
            ).exclude(pk__in=self._imported_service_ids)
            deleted_vs = orphaned_services.count()
            orphaned_services.delete()

            # ── IPAddress (VIP, Nodes, Self IPs) ──
            # Double filtre DB : préfixe [F5 + nom du device entre parenthèses
            device_tag = f'({self.device.name})'
            orphaned_ips = IPAddress.objects.filter(
                description__icontains='[F5',
            ).filter(
                description__icontains=device_tag,
            ).exclude(
                pk__in=self._imported_ip_ids,
            )
            deleted_ips = orphaned_ips.count()
            orphaned_ips.delete()

            # ── VLANs ── ne sont pas supprimés automatiquement
            # (pourraient être utilisés par d'autres objets NetBox)

            if deleted_vs or deleted_ips:
                self.stats['deleted'] = deleted_vs + deleted_ips
                logger.info(f'[F5] Nettoyage : {deleted_vs} VS + {deleted_ips} IPs supprimés')

        except Exception as e:
            logger.warning(f'[F5] Cleanup orphans : {e}')

    def _update_device(self):
        try:
            self._set_cf(self.device, {
                'f5_vs_count':     self.stats['virtual_servers'],
                'f5_pool_count':   self.stats['pools'],
                'f5_node_count':   self.stats['nodes'],
                'f5_vlan_count':   self.stats['vlans'],
                'f5_selfip_count': self.stats['self_ips'],
                'f5_last_sync':    timezone.now().isoformat(),
            })
            self.device.save(update_fields=['custom_field_data'])
        except Exception as e:
            logger.warning(f'[F5] Mise à jour device : {e}')
