"""Client API F5 BIG-IP iControl REST."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, Any, List
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger('netbox.plugins.netbox_f5_bigip')


class F5Client:
    """Client pour l'API iControl REST de F5 BIG-IP."""

    def __init__(self, host: str, username: str, password: str,
                 verify_ssl: bool = False, timeout: int = 30):
        self.host     = host.rstrip('/')
        self.base_url = f"https://{self.host}/mgmt/tm"
        self.timeout  = timeout

        self.session = requests.Session()
        self.session.auth   = (username, password)
        self.session.verify = verify_ssl
        self.session.headers.update({'Content-Type': 'application/json'})

        adapter = HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1,
                                                status_forcelist=[502, 503, 504]))
        self.session.mount("https://", adapter)

    def _get(self, endpoint: str, params: dict = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _collect(self, endpoint: str, params: dict = None) -> List[Dict[str, Any]]:
        """Appel générique qui retourne la liste items, ou [] si vide/erreur."""
        try:
            data = self._get(endpoint, params)
            items = data.get('items', [])
            logger.debug(f"F5 {endpoint} → {len(items)} items")
            return items
        except Exception as e:
            logger.error(f"F5 {endpoint} erreur : {e}")
            return []

    # ------------------------------------------------------------------ #
    #  LTM                                                                 #
    # ------------------------------------------------------------------ #

    def get_virtual_servers(self) -> List[Dict[str, Any]]:
        items = self._collect('ltm/virtual', {'expandSubcollections': 'true'})
        # Normaliser les profils et iRules
        for vs in items:
            if not isinstance(vs, dict):
                continue
            
            profiles_ref = vs.get('profilesReference', {})
            raw_profiles = profiles_ref.get('items', [])
            
            profiles = []
            if isinstance(raw_profiles, list):
                for p in raw_profiles:
                    if isinstance(p, dict):
                        name = p.get('name', '')
                        if name:
                            profiles.append(name)
            
            vs['profiles'] = profiles
            
            rules = vs.get('rules', [])
            if isinstance(rules, list):
                vs['rules'] = [r.split('/')[-1] for r in rules if isinstance(r, str)]
            else:
                vs['rules'] = []
        return items

    def get_pools(self) -> List[Dict[str, Any]]:
        """Récupère pools + membres en un seul appel."""
        items = self._collect('ltm/pool', {'expandSubcollections': 'true'})
        # Normaliser les membres inline
        for pool in items:
            if not isinstance(pool, dict):
                continue
            
            members_ref = pool.get('membersReference', {})
            raw_members = members_ref.get('items', [])
            
            members_list = []
            if isinstance(raw_members, list):
                for m in raw_members:
                    if isinstance(m, dict):
                        member_name = m.get('name', '')
                        members_list.append({
                            'name':    member_name,
                            'address': m.get('address', '').split('%')[0],
                            'port':    int(member_name.rsplit(':', 1)[-1])
                                       if ':' in member_name else 0,
                            'state':   m.get('state', 'unknown'),
                            'session': m.get('session', ''),
                        })
            
            pool['members_list'] = members_list
        return items

    def get_nodes(self) -> List[Dict[str, Any]]:
        return self._collect('ltm/node')

    # ------------------------------------------------------------------ #
    #  NET                                                                 #
    # ------------------------------------------------------------------ #

    def get_vlans(self) -> List[Dict[str, Any]]:
        vlans = self._collect('net/vlan', {'expandSubcollections': 'true'})
        # Normaliser les interfaces taggées
        for vlan in vlans:
            if not isinstance(vlan, dict):
                continue
            
            # interfacesReference contient les interfaces taggées avec ce VLAN
            interfaces_ref = vlan.get('interfacesReference', {})
            raw_interfaces = interfaces_ref.get('items', [])
            
            # Vérifier que raw_interfaces est bien une liste de dicts
            tagged = []
            if isinstance(raw_interfaces, list):
                for iface in raw_interfaces:
                    if isinstance(iface, dict):
                        if iface.get('tagged', False):
                            name = iface.get('name', '').split('/')[-1]
                            if name:
                                tagged.append(name)
            
            vlan['tagged_interfaces'] = tagged
        return vlans

    def get_self_ips(self) -> List[Dict[str, Any]]:
        return self._collect('net/self')

    def get_interfaces(self) -> List[Dict[str, Any]]:
        """Récupère les interfaces réseau (physiques)."""
        return self._collect('net/interface')

    def get_trunks(self) -> List[Dict[str, Any]]:
        """Récupère les agrégations (LACP trunks)."""
        trunks = self._collect('net/trunk')
        # Normaliser les membres
        for trunk in trunks:
            if not isinstance(trunk, dict):
                continue
            
            interfaces_ref = trunk.get('interfacesReference', {})
            raw_interfaces = interfaces_ref.get('items', [])
            
            # Vérifier que raw_interfaces est bien une liste de dicts
            members = []
            if isinstance(raw_interfaces, list):
                for iface in raw_interfaces:
                    if isinstance(iface, dict):
                        name = iface.get('name', '').split('/')[-1]
                        if name:
                            members.append(name)
            
            trunk['member_interfaces'] = members
        return trunks

    # ------------------------------------------------------------------ #
    #  Inventaire complet                                                  #
    # ------------------------------------------------------------------ #

    def fetch_full_inventory(self) -> Dict[str, Any]:
        return {
            'virtual_servers': self.get_virtual_servers(),
            'pools':           self.get_pools(),
            'nodes':           self.get_nodes(),
            'vlans':           self.get_vlans(),
            'self_ips':        self.get_self_ips(),
            'interfaces':      self.get_interfaces(),
            'trunks':          self.get_trunks(),
        }
