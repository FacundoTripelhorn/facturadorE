"""Launcher de FacturadorE: chooser, supervisión y lock de perfil.

FAC-28 supervisor · FAC-29 chooser · FAC-30 lock anti-duplicado ·
FAC-32 cambio de ambiente por reinicio.
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
from .switch import (
    CHANGE_ENVIRONMENT_REQUEST_FILENAME,
    LAUNCHER_SUPERVISED_ENV,
    ChangeEnvironmentRequest,
    change_environment_request_path,
    clear_change_environment_request,
    is_launcher_supervised,
    read_change_environment_request,
    write_change_environment_request,
)

__all__ = [
    "CHANGE_ENVIRONMENT_REQUEST_FILENAME",
    "ENVIRONMENT_OPTIONS",
    "LAUNCHER_SUPERVISED_ENV",
    "BackendLaunchPlan",
    "ChangeEnvironmentRequest",
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
    "change_environment_request_path",
    "choose_environment",
    "clear_change_environment_request",
    "environment_options",
    "is_launcher_supervised",
    "is_process_alive",
    "plan_backend_launch",
    "prompt_environment_gui",
    "prompt_environment_tty",
    "read_change_environment_request",
    "read_lock_holder",
    "resolve_launch_environment",
    "resolve_launch_profile",
    "start_backend",
    "write_change_environment_request",
]
