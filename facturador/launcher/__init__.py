"""Launcher de FacturadorE: supervisión de un backend por perfil (FAC-28)."""

from .command import (
    BackendLaunchPlan,
    build_backend_command,
    build_backend_env,
    plan_backend_launch,
    resolve_launch_environment,
    resolve_launch_profile,
)
from .supervisor import LauncherError, ProcessSupervisor, start_backend

__all__ = [
    "BackendLaunchPlan",
    "LauncherError",
    "ProcessSupervisor",
    "build_backend_command",
    "build_backend_env",
    "plan_backend_launch",
    "resolve_launch_environment",
    "resolve_launch_profile",
    "start_backend",
]
