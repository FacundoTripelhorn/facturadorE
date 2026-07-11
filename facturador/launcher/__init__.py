"""Launcher de FacturadorE: chooser, supervisión y lock de perfil.

FAC-28 supervisor · FAC-29 chooser · FAC-30 lock anti-duplicado.
"""

from .chooser import (
    ENVIRONMENT_OPTIONS,
    ChooserUnavailable,
    EnvironmentOption,
    choose_environment,
    environment_options,
    prompt_environment_gui,
    prompt_environment_tty,
)
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
    "ENVIRONMENT_OPTIONS",
    "BackendLaunchPlan",
    "ChooserUnavailable",
    "EnvironmentOption",
    "LaunchResult",
    "LauncherError",
    "LockHolder",
    "ProcessSupervisor",
    "ProfileLock",
    "ProfileLockError",
    "ProfileLockHeld",
    "build_backend_command",
    "build_backend_env",
    "choose_environment",
    "environment_options",
    "is_process_alive",
    "plan_backend_launch",
    "prompt_environment_gui",
    "prompt_environment_tty",
    "read_lock_holder",
    "resolve_launch_environment",
    "resolve_launch_profile",
    "start_backend",
]
