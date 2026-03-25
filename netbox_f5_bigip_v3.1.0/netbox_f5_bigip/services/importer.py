"""Importer F5 → NetBox avec synchronisation bidirectionnelle."""
import logging
import traceback
import json
import re
from typing import Dict, Any, Optional, Set
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from dcim.models import Device, Interface
from virtualization.models import VirtualMachine, VMInterface
from ipam.models import Service, IPAddress, VLAN, Prefix

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
        self._ip_cache = {}
        
        # Tracking pour synchronisation
        self._imported_service_ids: Set[int] = set()
        self._imported_ip_ids: Set[int] = set()
        self._imported_vlan_ids: Set[int] = set()
        
        # Mapping interfaces F5 → NetBox et trunks
        self._interface_map: Dict[str, Any] = {}  # 'eth1' → Interface NetBox
        self._trunk_members: Dict[str, list] = {}  # 'trunk1' → ['eth1', 'eth2']
        self._vlan_cache: Dict[int, VLAN] = {}  # VID → VLAN NetBox

    # ── Utilitaires ─────────────────────────────────────────────────────── #

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

    def _get_or_create_ip(self, address: str, description: str) -> Optional[IPAddress]:
        if not address or address in ('any', '0.0.0.0'):
            return None
        addr = address.split('%')[0]
        if '/' not in addr:
            addr += '/32'
        if addr in self._ip_cache:
            return IPAddress.objects.filter(pk=self._ip_cache[addr]).first()
        try:
            obj, created = IPAddress.objects.get_or_create(
                address=addr,
                defaults={'description': description}
            )
            if not created and description and not obj.description:
                obj.description = description
                obj.save(update_fields=['description'])
            self._ip_cache[addr] = obj.pk
            self._imported_ip_ids.add(obj.pk)
            return obj
        except Exception as e:
            logger.error(f'IP {addr} : {e}')
            return None

    def _set_cf(self, obj, updates: dict):
        # Protéger contre custom_field_data qui serait une liste au lieu d'un dict
        if isinstance(obj.custom_field_data, dict):
            cf = dict(obj.custom_field_data)
        else:
            cf = {}
        cf.update(updates)
        obj.custom_field_data = cf

    def _save_service(self, name: str, vip: str, port: int,
                      protocol: str, description: str) -> Optional[Service]:
        """
        Crée ou met à jour un Service avec nom formaté : "VS_NAME (IP:PORT)"
        Compatible NetBox 4.6+ avec GenericForeignKey parent_object_type/id.
        """
        # Format du nom : "VS_NAME (IP:PORT)"
        display_name = f"{name} ({vip}:{port})" if vip else name

        if self.kind == 'vm':
            ct = ContentType.objects.get_for_model(VirtualMachine)
        else:
            ct = ContentType.objects.get_for_model(Device)

        # Chercher par nom original (pour retrouver les anciens)
        qs = Service.objects.filter(
            name=name,
            parent_object_type=ct,
            parent_object_id=self.device.pk,
        )

        # Sinon chercher par nom formaté
        if not qs.exists():
            qs = Service.objects.filter(
                name=display_name,
                parent_object_type=ct,
                parent_object_id=self.device.pk,
            )

        if qs.exists():
            service = qs.first()
            service.name        = display_name  # mise à jour du nom
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
        """Crée l'IPAddress VIP et la lie au Service via M2M ipaddresses."""
        if not ip_str or ip_str in ('any', '0.0.0.0'):
            return

        vip = self._get_or_create_ip(ip_str, f'[F5 VIP] {vs_name} ({self.device.name})')
        if not vip:
            return

        try:
            service.ipaddresses.add(vip)
        except Exception as e:
            logger.debug(f'ipaddresses.add() : {e}')

    def _associate_vlan_to_prefix(self, vlan: VLAN):
        """
        Associe automatiquement le VLAN créé à un préfixe existant dans NetBox
        en cherchant les préfixes qui correspondent au VLAN tag.
        """
        try:
            # Chercher les préfixes qui ont ce VLAN tag dans leurs custom fields
            # ou qui correspondent au sous-réseau du VLAN
            # Pour l'instant : association simple via VLAN ID
            prefixes = Prefix.objects.filter(vlan__isnull=True)
            
            # Essayer de trouver un préfixe candidat
            # (logique à affiner selon votre nommage)
            # Par exemple : chercher un préfixe dont le nom contient le nom du VLAN
            candidates = prefixes.filter(description__icontains=vlan.name)[:1]
            
            if candidates:
                prefix = candidates[0]
                prefix.vlan = vlan
                prefix.save(update_fields=['vlan'])
                logger.info(f'[F5] VLAN {vlan.name} associé au préfixe {prefix}')
        except Exception as e:
            logger.debug(f'Association VLAN→Prefix : {e}')

    def _build_interface_mapping(self, interfaces: list, trunks: list):
        """
        Construit le mapping entre interfaces F5 et interfaces NetBox.
        Gère aussi les trunks/agrégations.
        
        Format F5 : "1.1", "1.2", "trunk1"
        Format NetBox : cherche par nom dans l'équipement
        """
        # 1. Mapper les trunks vers leurs membres
        if isinstance(trunks, list):
            for trunk in trunks:
                if not isinstance(trunk, dict):
                    continue
                trunk_name = trunk.get('name', '').split('/')[-1]  # Ex: "trunk1"
                members = trunk.get('member_interfaces', [])  # Ex: ["1.1", "1.2"]
                if trunk_name and members:
                    self._trunk_members[trunk_name] = members
                    logger.debug(f'[F5] Trunk {trunk_name} → {members}')
        
        # 2. Mapper les interfaces F5 vers interfaces NetBox
        if self.kind == 'vm':
            netbox_interfaces = VMInterface.objects.filter(virtual_machine=self.device)
        else:
            netbox_interfaces = Interface.objects.filter(device=self.device)
        
        for iface in netbox_interfaces:
            # Normaliser le nom NetBox pour matcher F5
            # Ex: "eth1" → "1.1", "GigabitEthernet0/1" → "0/1"
            f5_name = self._normalize_interface_name(iface.name)
            if f5_name:
                self._interface_map[f5_name] = iface
                logger.debug(f'[F5] Mapping {f5_name} (F5) → {iface.name} (NetBox)')

    def _normalize_interface_name(self, name: str) -> str:
        """
        Convertit un nom d'interface NetBox vers le format F5.
        Ex: "eth1" → "1.1", "GigabitEthernet1/1" → "1.1"
        """
        # Patterns courants F5 : "1.1", "1.2", "2.1"
        # Essayer d'extraire les chiffres
        match = re.search(r'(\d+)[./](\d+)', name)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
        
        # Si c'est juste "eth1" → "1.1"
        match = re.search(r'eth(\d+)', name.lower())
        if match:
            return f"1.{match.group(1)}"
        
        # Sinon retourner tel quel
        return name

    def _resolve_f5_interface_to_netbox(self, f5_interface: str) -> list:
        """
        Résout une interface F5 (qui peut être un trunk) vers les interfaces NetBox.
        
        Args:
            f5_interface: Nom interface F5, ex: "1.1" ou "trunk1"
        
        Returns:
            Liste d'interfaces NetBox correspondantes
        """
        # Si c'est un trunk, résoudre vers les membres
        if f5_interface in self._trunk_members:
            netbox_interfaces = []
            for member in self._trunk_members[f5_interface]:
                if member in self._interface_map:
                    netbox_interfaces.append(self._interface_map[member])
            return netbox_interfaces
        
        # Sinon chercher directement l'interface
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

        logger.info(f'Import F5 → {self.device.name}')

        # 1. Construire le mapping des interfaces et trunks
        interfaces = inventory.get('interfaces', [])
        trunks = inventory.get('trunks', [])
        self._build_interface_mapping(interfaces, trunks)

        # 2. Importer nodes, vlans, self IPs
        for node in inventory.get('nodes', []):
            if not isinstance(node, dict):
                continue
            self._import_node(node)

        for vlan in inventory.get('vlans', []):
            if not isinstance(vlan, dict):
                continue
            self._import_vlan(vlan)

        for selfip in inventory.get('self_ips', []):
            if not isinstance(selfip, dict):
                continue
            self._import_self_ip(selfip)

        # 3. Importer pools et VS
        pools_by_name = {}
        for pool in inventory.get('pools', []):
            if not isinstance(pool, dict):
                continue
            name = pool.get('name', '')
            if name:
                pools_by_name[name] = pool
                self.stats['pools'] += 1

        for vs in inventory.get('virtual_servers', []):
            if not isinstance(vs, dict):
                continue
            self._import_vs(vs, pools_by_name)

        # ── Synchronisation : supprimer les objets orphelins ──────────────
        self._cleanup_orphaned_objects()

        self._update_device()
        logger.info(f'Import terminé : {self.stats}')
        return self.stats

    # ── Objets individuels ───────────────────────────────────────────────── #

    def _import_node(self, data: dict):
        name    = data.get('name', '')
        address = data.get('address', '').split('%')[0]
        if not name or not address or address == 'any':
            return
        try:
            with transaction.atomic():
                if self._get_or_create_ip(address, f'[F5 Node] {name} ({self.device.name})'):
                    self.stats['nodes'] += 1
        except Exception as e:
            logger.error(f'Node {name} : {e}')
            self.stats['errors'] += 1

    def _import_vlan(self, data: dict):
        name = data.get('name', '')
        tag  = data.get('tag')
        tagged_interfaces = data.get('tagged_interfaces', [])  # Interfaces F5 taggées
        
        if not name:
            return
        try:
            with transaction.atomic():
                vid = int(tag) if tag else None
                if not vid or not (1 <= vid <= 4094):
                    return
                
                # Chercher VLAN existant par VID (ordre de priorité)
                # 1. Même tenant que le device (si tenant défini)
                # 2. Sans tenant
                # 3. N'importe lequel avec ce VID
                vlan = None
                
                device_tenant = getattr(self.device, 'tenant', None)
                
                if device_tenant:
                    # Chercher dans le même tenant d'abord
                    vlan = VLAN.objects.filter(vid=vid, tenant=device_tenant).first()
                
                if not vlan:
                    # Chercher sans tenant
                    vlan = VLAN.objects.filter(vid=vid, tenant__isnull=True).first()
                
                if not vlan:
                    # Dernier recours : n'importe quel VLAN avec ce VID
                    vlan = VLAN.objects.filter(vid=vid).first()
                
                if vlan:
                    # VLAN existe déjà → RÉUTILISER sans modifier le nom
                    logger.info(f'[F5] VLAN {vid} existant réutilisé : "{vlan.name}" (nom conservé)')
                else:
                    # Créer nouveau VLAN uniquement si aucun n'existe
                    vlan = VLAN(
                        vid=vid,
                        name=name,
                        tenant=device_tenant if device_tenant else None
                    )
                    vlan.save()
                    logger.info(f'[F5] VLAN créé : {name} (VID {vid})')
                    # Essayer d'associer à un préfixe existant
                    self._associate_vlan_to_prefix(vlan)
                
                # Stocker dans cache pour les self IPs
                self._vlan_cache[vid] = vlan
                self._imported_vlan_ids.add(vlan.pk)
                self.stats['vlans'] += 1
                
                # Associer les interfaces NetBox correspondantes
                if isinstance(tagged_interfaces, list):
                    for f5_iface in tagged_interfaces:
                        if not isinstance(f5_iface, str):
                            continue
                        netbox_ifaces = self._resolve_f5_interface_to_netbox(f5_iface)
                        for nb_iface in netbox_ifaces:
                            # Ajouter le VLAN aux tagged_vlans de l'interface
                            if hasattr(nb_iface, 'tagged_vlans'):
                                if vlan not in nb_iface.tagged_vlans.all():
                                    nb_iface.tagged_vlans.add(vlan)
                                    logger.info(f'[F5] VLAN {vid} ajouté à {nb_iface.name}')
                                else:
                                    logger.debug(f'[F5] VLAN {vid} déjà présent sur {nb_iface.name}')
                
        except Exception as e:
            logger.error(f'VLAN {name} (VID {tag}) : {e}')
            self.stats['errors'] += 1

    def _import_self_ip(self, data: dict):
        name    = data.get('name', '')
        address = data.get('address', '').split('%')[0]
        vlan_ref = data.get('vlan', '')  # Ex: "/Common/vlan100"
        
        if not name or not address:
            return
        
        try:
            with transaction.atomic():
                # 1. Trouver le VLAN correspondant
                vlan_name = vlan_ref.split('/')[-1] if vlan_ref else None
                vlan = None
                
                # Chercher le VLAN par nom dans ceux qu'on a importés
                if vlan_name:
                    for vid, v in self._vlan_cache.items():
                        # Matcher le nom F5 avec le nom NetBox (ou l'ancien nom)
                        if vlan_name.lower() in v.name.lower() or str(vid) in vlan_name:
                            vlan = v
                            break
                
                # 2. Créer ou récupérer l'IP
                if '/' not in address:
                    address += '/32'
                
                ip_obj = IPAddress.objects.filter(address=address).first()
                
                if not ip_obj:
                    # Créer nouvelle IP
                    ip_obj = IPAddress(
                        address=address,
                        description=f'[F5 Self IP] {name} ({self.device.name})',
                        vrf=None,
                    )
                    ip_obj.save()
                    logger.info(f'[F5] Self IP créée : {address} → VLAN {vlan.name if vlan else "N/A"}')
                else:
                    # IP existe déjà → réutiliser
                    logger.info(f'[F5] Self IP existante réutilisée : {address}')
                    # Mettre à jour la description si nécessaire
                    new_desc = f'[F5 Self IP] {name} ({self.device.name})'
                    if ip_obj.description != new_desc:
                        ip_obj.description = new_desc
                        ip_obj.save(update_fields=['description'])
                
                # 3. Associer au VLAN si trouvé
                if vlan and ip_obj.vlan != vlan:
                    ip_obj.vlan = vlan
                    ip_obj.save(update_fields=['vlan'])
                    logger.info(f'[F5] IP {address} associée au VLAN {vlan.name}')
                
                # 4. Associer à l'interface NetBox
                # Chercher quelle interface porte ce VLAN
                if vlan:
                    # Chercher l'interface qui a ce VLAN en tagged
                    if self.kind == 'vm':
                        candidate_ifaces = VMInterface.objects.filter(
                            virtual_machine=self.device,
                            tagged_vlans=vlan
                        )
                    else:
                        candidate_ifaces = Interface.objects.filter(
                            device=self.device,
                            tagged_vlans=vlan
                        )
                    
                    if candidate_ifaces.exists():
                        target_iface = candidate_ifaces.first()
                        if ip_obj.assigned_object != target_iface:
                            ip_obj.assigned_object = target_iface
                            ip_obj.save()
                            logger.info(f'[F5] IP {address} assignée à {target_iface.name}')
                
                self._imported_ip_ids.add(ip_obj.pk)
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

        # Format JSON sans state/session (pas de monitoring dans NetBox)
        # Sécuriser : vérifier que members est une liste de dicts
        members_clean = []
        if isinstance(members, list):
            for m in members:
                if isinstance(m, dict):
                    members_clean.append({
                        'address': m.get('address', ''),
                        'port': m.get('port', 0)
                    })
        members_json = json.dumps(members_clean)

        # Sécuriser profiles et rules
        profiles = data.get('profiles', [])
        if isinstance(profiles, list):
            profiles_str = ', '.join([str(p) for p in profiles if p])
        else:
            profiles_str = ''

        rules = data.get('rules', [])
        if isinstance(rules, list):
            rules_str = ', '.join([str(r) for r in rules if r])
        else:
            rules_str = ''

        # Sécuriser sourceAddressTranslation
        snat_data = data.get('sourceAddressTranslation', {})
        if isinstance(snat_data, dict):
            snat_type = snat_data.get('type', '')
        else:
            snat_type = ''

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

                # Lier le VIP au Service
                self._link_vip_to_service(service, ip_str, name)

                self.stats['virtual_servers'] += 1

        except Exception as e:
            logger.error(f'VS {name} : {e} | {traceback.format_exc().splitlines()[-2]}')
            self.stats['errors'] += 1

    def _cleanup_orphaned_objects(self):
        """
        Supprime les objets NetBox qui ne sont plus présents dans la config F5.
        """
        try:
            if self.kind == 'vm':
                ct = ContentType.objects.get_for_model(VirtualMachine)
            else:
                ct = ContentType.objects.get_for_model(Device)

            # ── Services (Virtual Servers) ──
            all_services = Service.objects.filter(
                parent_object_type=ct,
                parent_object_id=self.device.pk,
            )
            orphaned_services = [s for s in all_services if s.pk not in self._imported_service_ids]
            deleted_vs = len(orphaned_services)
            for s in orphaned_services:
                s.delete()

            # ── IPAddress (VIP, Nodes, Self IPs) ──
            # Supprimer les IPs avec description "[F5 *] ... (device_name)"
            pattern = f'({self.device.name})'
            all_f5_ips = IPAddress.objects.filter(description__icontains='[F5')
            device_f5_ips = [ip for ip in all_f5_ips if pattern in (ip.description or '')]
            orphaned_ips = [ip for ip in device_f5_ips if ip.pk not in self._imported_ip_ids]
            deleted_ips = len(orphaned_ips)
            for ip in orphaned_ips:
                ip.delete()

            # ── VLANs ──
            # Ne pas supprimer automatiquement les VLANs (utilisés ailleurs)
            # Mais on pourrait logger ceux qui ne sont plus présents
            
            if deleted_vs or deleted_ips:
                self.stats['deleted'] = deleted_vs + deleted_ips
                logger.info(f'[F5] Nettoyage : {deleted_vs} VS + {deleted_ips} IPs supprimés')

        except Exception as e:
            logger.warning(f'Cleanup orphans : {e}')

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
            logger.warning(f'Mise à jour device : {e}')
