"""Tâches de fond F5 — exécutées par netbox-rq (Redis Queue)."""
import logging
from django_rq import job

logger = logging.getLogger('netbox.plugins.netbox_f5_bigip')


@job('default')
def run_f5_import(device_id: int, kind: str, inventory: dict):
    """
    Tâche RQ : import F5 en arrière-plan.
    Retourne le dict stats (stocké dans le job result par RQ).
    """
    from netbox_f5_bigip.services.importer import F5Importer
    logger.info(f'[F5 Job] Démarrage import {kind}/{device_id}')
    importer = F5Importer(device_id=device_id, kind=kind)
    stats = importer.import_inventory(inventory)
    logger.info(f'[F5 Job] Terminé {kind}/{device_id} : {stats}')
    return stats
