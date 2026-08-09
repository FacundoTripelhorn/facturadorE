"""Ensamblado de la app FastAPI: wiring de dependencias, manejo de errores
de dominio y registro de routers (API JSON + frontend HTML §2.4).
Servida solo en localhost (design.md §2.5)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db, web
from ..arca.wsfex import WsfexClient
from ..config import Config, load_config
from ..profile import EnvironmentProfile, ProfileError
from ..seed_backup_sync import SeedBackupCoordinator
from ..service import (
    ArcaUnavailableError,
    ConflictError,
    DomainError,
    InvoiceService,
    NotFoundError,
)
from ..setup import make_setup_state_provider, reconcile_setup_state
from . import backup, clients, health, invoices, params, registry, setup
from .csrf import CsrfCookieMiddleware, CsrfRejected
from .localhost_policy import (
    LocalhostPolicyMiddleware,
    resolve_listen_port,
    resolve_policy_ports,
)
from .setup_guard import SetupGuardMiddleware

_ERROR_STATUS = {
    NotFoundError: 404,
    ConflictError: 409,
    DomainError: 422,
    ArcaUnavailableError: 503,
}


def create_app(
    profile: EnvironmentProfile,
    config: Config | None = None,
    conn: sqlite3.Connection | None = None,
    wsfex: WsfexClient | None = None,
    *,
    port: int | None = None,
    public_port: int | None = None,
    seed_backup: SeedBackupCoordinator | None = None,
) -> FastAPI:
    """Construye la app contra UN perfil de ambiente explícito e inmutable.

    ADR 0001 / FAC-24: el ambiente se decide antes de crear FastAPI, SQLite
    y los clientes ARCA, y no existe forma de cambiarlo después (perfil y
    Config son frozen; no hay endpoint ni setter). URLs de ARCA, certificados
    y paths derivan todos del perfil vía Config/ProfilePaths (FAC-25).

    FAC-41: ``port`` (o ``FACTURADOR_PORT``) es el bind; la allowlist de
    Host/Origin usa ese puerto y, si aplica, ``public_port`` /
    ``FACTURADOR_PUBLIC_PORT`` (publish del host en Docker cuando difiere
    del 8399 interno). FAC-42: cookie CSRF emitida en respuestas; los
    POST ``/ui/…`` la validan vía dependency del router HTML. FAC-35:
    setup state por perfil + guardia que bloquea factura/ARCA hasta
    ``ready`` (``GET /health`` y ``/setup`` quedan libres). FAC-47: seed
    backup coordinator (config-change trigger + retry on launch).
    """
    if config is None:
        config = load_config(profile)
    elif config.env != profile.environment:
        raise ProfileError(
            f"Config ({config.env}) y perfil ({profile.environment}) no "
            "coinciden: el backend corre contra exactamente un ambiente."
        )
    elif config.paths.root != profile.paths.root:
        raise ProfileError(
            "Config y perfil no coinciden: los paths de la Config no salen "
            "de la raíz del perfil inyectado."
        )
    conn = conn or db.connect(config.paths.db)
    wsfex = wsfex or WsfexClient(config)
    listen_port = resolve_listen_port(port)
    policy_ports = resolve_policy_ports(listen_port, public_port)
    # Sin callback: un perfil ya ready no dispara backup en cada arranque.
    setup_state = reconcile_setup_state(profile, conn)

    coordinator = seed_backup or SeedBackupCoordinator(
        conn,
        profile.paths,
        environment=profile.environment.value,
    )

    def _on_became_ready() -> None:
        coordinator.notify_config_changed("onboarding_completed")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Retry pending/failed seed upload from a prior session (FAC-47).
        try:
            coordinator.retry_if_pending()
        except Exception:
            # Cinturón: retry_if_pending ya no debería propagar.
            pass
        yield
        coordinator.shutdown()

    app = FastAPI(title="facturador", version="0.1.0", lifespan=lifespan)
    app.state.profile = profile
    app.state.seed_backup = coordinator
    app.state.service = InvoiceService(
        config, conn, wsfex, seed_backup=coordinator
    )
    app.state.listen_port = listen_port
    app.state.policy_ports = policy_ports
    app.state.setup_state = setup_state

    # Setup guard (FAC-35) + CSRF (FAC-42) + Host/Origin (FAC-41).
    # add_middleware apila por fuera: el último agregado es el más externo.
    # Orden de request: LocalhostPolicy → CsrfCookie → SetupGuard → routers.
    app.add_middleware(
        SetupGuardMiddleware,
        get_state=make_setup_state_provider(
            profile, conn, on_became_ready=_on_became_ready
        ),
    )
    app.add_middleware(CsrfCookieMiddleware)
    app.add_middleware(LocalhostPolicyMiddleware, ports=policy_ports)

    def _handler_for(status: int):
        async def handler(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    for tipo, status in _ERROR_STATUS.items():
        app.add_exception_handler(tipo, _handler_for(status))

    # FAC-62: CSRF fallido en formularios /ui/ → HTML; JSON fuera de /ui/.
    app.add_exception_handler(CsrfRejected, web.csrf_rejected_handler)

    app.include_router(invoices.router)
    app.include_router(clients.router)
    app.include_router(params.router)
    app.include_router(health.router)
    app.include_router(setup.router)
    app.include_router(registry.router)
    app.include_router(backup.router)

    # Frontend HTML (§2.4): mismas dependencias vía app.state.service. Los
    # errores de dominio del frontend se renderizan en partials, no acá.
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")

    return app
