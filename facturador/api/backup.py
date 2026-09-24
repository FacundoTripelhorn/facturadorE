"""Seed backup status + manual trigger. Feeds the diagnostics view."""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import SeedBackupStateOut
from ..seed_backup_sync import seed_backup_state_as_dict
from .deps import ServiceDep

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/seed", response_model=SeedBackupStateOut)
def get_seed_backup_state(service: ServiceDep) -> SeedBackupStateOut:
    """Queryable last-success / pending / failed state."""
    state = service.get_seed_backup_state()
    return SeedBackupStateOut(**seed_backup_state_as_dict(state))


@router.post("/seed", response_model=SeedBackupStateOut)
def run_seed_backup_now(service: ServiceDep) -> SeedBackupStateOut:
    """Manual "backup now" — create local seed and upload if S3 is configured."""
    state = service.run_seed_backup_now()
    return SeedBackupStateOut(**seed_backup_state_as_dict(state))
