"""Launcher de FacturadorE: supervisión y lock de perfil (FAC-28 / FAC-30)."""

from .command import (
    BackendLaunchPlan,
    build_backend_command,
    build_backend_env,
    plan_backend_launch,
    resolve_launch_environment,
    resolve_launch_profile,
)
from .lock import (
    LockHolder,
    ProfileLock,
    ProfileLockError,
    ProfileLockHeld,
    is_process_alive,
    read_lock_holder,
)
from .supervisor import LauncherError, LaunchResult, ProcessSupervisor, start_backend

__all__ = [
    "BackendLaunchPlan",
    "LaunchResult",
    "LauncherError",
    "LockHolder",
    "ProcessSupervisor",
    "ProfileLock",
    "ProfileLockError",
    "ProfileLockHeld",
    "build_backend_command",
    "build_backend_env",
    "is_process_alive",
    "plan_backend_launch",
    "read_lock_holder",
    "resolve_launch_environment",
    "resolve_launch_profile",
    "start_backend",
]
