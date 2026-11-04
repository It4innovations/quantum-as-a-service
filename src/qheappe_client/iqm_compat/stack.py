"""Detect which IQM software stack is installed and which one a backend needs.

Supported stacks (only one can be installed at a time):

========  ==========  ===========================  ===================
Stack     iqm-client  iqm-station-control-client   qaas extra
========  ==========  ===========================  ===================
IQM36     36.x        14.x                         ``iqm_qiskit``
IQM35     35.x        13.x                         ``iqm35_qiskit``
========  ==========  ===========================  ===================

The backend is identified by ``version.station_control_version`` from
``/heappe/QScheduler/MachineInfo``. This is the version of IQM's station-control
*service* on the server (e.g. ``49.0.3``), not of the client library. Its major
version is looked up in :data:`STATION_CONTROL_MAJOR_TO_STACKS`; an unknown
major only logs a warning and the installed stack is used.

==================  ==========================================
station-control     IQM stacks that work (recommended first)
==================  ==========================================
50.x (current)      IQM36
49.x                IQM36 (tested live), IQM35
==================  ==========================================

``iqm_qiskit`` (IQM36) is the recommended extra for every backend;
``iqm35_qiskit`` is only needed for code that uses Pulla (e.g. the legacy
``qaas`` client) and works with 49.x backends only.

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

# Station-control *service* major (reported by the backend) -> stacks that work
# with it, recommended first. Add newer backend versions here once known.
STATION_CONTROL_MAJOR_TO_STACKS: dict[int, tuple[IQMStack, ...]] = {
    49: (IQMStack.IQM36, IQMStack.IQM35),  # older backends, e.g. VLQ 49.0.3
    50: (IQMStack.IQM36,),  # current backends
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


# Package index that hosts qaas (it is not on public PyPI).
QAAS_EXTRA_INDEX_URL = (
    "https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple"
)

# Top-level modules provided by the IQM extras.
IQM_EXTRA_MODULES = frozenset({"qiskit", "iqm", "exa"})


def install_hint(stack: IQMStack) -> str:
    """Pip command that installs the given stack."""
    return (
        f"pip install 'qaas[{STACK_EXTRA[stack]}]' "
        f"--extra-index-url {QAAS_EXTRA_INDEX_URL}"
    )


def is_iqm_extra_import_error(error: ImportError) -> bool:
    """True if ``error`` comes from a module that only the IQM extras provide."""
    return (error.name or "").split(".")[0] in IQM_EXTRA_MODULES


def iqm_import_error_message(error: ImportError) -> str:
    """Explain an import error from the IQM libraries and how to fix it.

    Args:
        error: The ImportError raised while importing an IQM/Qiskit module.

    Returns:
        Message naming the problem and the extras to install.
    """
    version = installed_iqm_client_version()
    if version is None:
        problem = "The IQM libraries (qiskit, iqm-client) are not installed."
    elif _major(version) not in CLIENT_MAJOR_TO_STACK:
        supported = ", ".join(f"{m}.x" for m in CLIENT_MAJOR_TO_STACK)
        problem = (
            f"Installed iqm-client {version} is not supported (supported: {supported})."
        )
    else:
        problem = (
            f"The IQM libraries are incomplete or mixed (iqm-client {version}); "
            "reinstall them with one of the commands below."
        )
    return (
        f"qheappe_client cannot import '{error.name}': {problem}\n"
        f"Install one IQM extra:\n"
        f"  recommended (station-control 49.x and 50.x): {install_hint(IQMStack.IQM36)}\n"
        f"  with Pulla  (station-control 49.x only):     {install_hint(IQMStack.IQM35)}"
    )


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


def compatible_stacks(machine_info: QMachineInfo | None) -> tuple[IQMStack, ...]:
    """Stacks that work with the backend, recommended first.

    Args:
        machine_info: Parsed ``MachineInfo`` of the backend.

    Returns:
        The compatible stacks, or ``()`` if the backend does not report a
        station-control version or reports one this client does not know.
    """
    if machine_info is None:
        return ()
    return STATION_CONTROL_MAJOR_TO_STACKS.get(
        _major(machine_info.station_control_version), ()
    )


def required_stack(machine_info: QMachineInfo | None) -> IQMStack | None:
    """Recommended stack for the backend, or ``None`` if its version is unknown."""
    stacks = compatible_stacks(machine_info)
    return stacks[0] if stacks else None
