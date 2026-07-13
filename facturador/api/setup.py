"""Estado de setup del perfil activo (FAC-35). Diagnóstico seguro, sin ARCA."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..setup import SetupState, is_ready, reconcile_setup_state
from .deps import ServiceDep

router = APIRouter(tags=["setup"])


@router.get("/setup")
def setup_status(request: Request, service: ServiceDep) -> dict[str, object]:
    """Paso actual del onboarding del perfil (reanudable tras reinicio)."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)
    request.app.state.setup_state = state
    return {
        "state": state.value,
        "ready": is_ready(state),
        "environment": profile.environment.value,
        "environment_label": profile.display_name,
        "steps": [s.value for s in SetupState if s is not SetupState.UNINITIALIZED],
    }
