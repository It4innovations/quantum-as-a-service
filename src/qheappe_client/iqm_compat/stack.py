"""Detect which IQM software stack is installed and which one a backend needs.

Supported stacks (only one can be installed at a time):

========  ===================  ===========================  =====================
Stack     iqm-client           iqm-station-control-client   qaas extra
========  ===================  ===========================  =====================
IQM35     35.x                 13.x                         ``iqm35_qiskit``
IQM36     36.x                 14.x                         ``iqm_qiskit``
========  ===================  ===========================  =====================

The backend side is identified by ``version.station_control_version`` from
``/heappe/QScheduler/MachineInfo``. This mirrors IQM's own compatibility check,
which compares the *major* version of ``iqm-station-control-client`` on client
and server.

This module imports no IQM packages, so it works with any (or no) stack installed.
"""

from __future__ import annotations

import re
from enum import StrEnum
from importlib import metadata

from ..backend_metadata import QMachineInfo
from ..utils import QException


class IQMStack(StrEnum):
    """IQM client-library generation; the value is the adapter module name."""

    IQM35 = "iqm35"
    IQM36 = "iqm36"


# iqm-client major -> stack
CLIENT_MAJOR_TO_STACK: dict[int, IQMStack] = {
    35: IQMStack.IQM35,
    36: IQMStack.IQM36,
}

# iqm-station-control-client major (reported by the backend) -> stack
STATION_CONTROL_MAJOR_TO_STACK: dict[int, IQMStack] = {
    13: IQMStack.IQM35,
    14: IQMStack.IQM36,
}

# stack -> qaas optional-dependency group that installs it
STACK_EXTRA: dict[IQMStack, str] = {
    IQMStack.IQM35: "iqm35_qiskit",
    IQMStack.IQM36: "iqm_qiskit",
}


def _major(version: str | None) -> int | None:
    """Major number of a version string like ``"14.0.0"``, ``"v13.2"`` or ``"14.0.0 (local editable)"``."""
    if not version:
        return None
    m = re.match(r"\s*v?(\d+)", str(version))
    return int(m.group(1)) if m else None


def install_hint(stack: IQMStack) -> str:
    """Pip command that installs the given stack."""
    return f"pip install 'qaas[{STACK_EXTRA[stack]}]'"


def installed_iqm_client_version() -> str | None:
    """Installed ``iqm-client`` version, or ``None`` if not installed."""
    try:
        return metadata.version("iqm-client")
    except metadata.PackageNotFoundError:
        return None


def installed_stack() -> IQMStack:
    """Stack matching the installed ``iqm-client``.

    Raises:
        QException: ``iqm-client`` is not installed, or its major version is not supported.
    """
    version = installed_iqm_client_version()
    if version is None:
        raise QException(
            "IQM backends need the 'iqm-client' package. "
            f"Install it with: {install_hint(IQMStack.IQM36)} "
            f"(or {install_hint(IQMStack.IQM35)} for older backends)."
        )
    stack = CLIENT_MAJOR_TO_STACK.get(_major(version))
    if stack is None:
        supported = ", ".join(f"{m}.x" for m in CLIENT_MAJOR_TO_STACK)
        raise QException(
            f"Installed iqm-client {version} is not supported (supported: {supported})."
        )
    return stack


def required_stack(machine_info: QMachineInfo | None) -> IQMStack | None:
    """Stack the backend needs, from its reported station-control version.

    Args:
        machine_info: Parsed ``MachineInfo`` of the backend.

    Returns:
        The required stack, or ``None`` if the backend does not report a
        station-control version or reports one this client does not know.
    """
    if machine_info is None:
        return None
    return STATION_CONTROL_MAJOR_TO_STACK.get(
        _major(machine_info.station_control_version)
    )
