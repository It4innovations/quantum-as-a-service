"""Support for several IQM client-library generations.

Only one generation can be installed at a time:

* ``qaas[iqm_qiskit]``    -> iqm-client 36.x (``IQMStack.IQM36``), station-control 49.x and 50.x (recommended)
* ``qaas[iqm35_qiskit]``  -> iqm-client 35.x (``IQMStack.IQM35``), station-control 49.x only (Pulla)

:class:`QBackendIQMFactory` checks the installed one against the version the
backend reports and builds the matching backend. The adapter modules
(``iqm35``, ``iqm36``) are imported lazily, so this package imports with
either stack installed.
"""

from .factory import QBackendIQMFactory
from .stack import (
    CLIENT_MAJOR_TO_STACK,
    STACK_EXTRA,
    STATION_CONTROL_MAJOR_TO_STACKS,
    IQMStack,
    compatible_stacks,
    installed_stack,
    required_stack,
)

__all__ = [
    "CLIENT_MAJOR_TO_STACK",
    "STACK_EXTRA",
    "STATION_CONTROL_MAJOR_TO_STACKS",
    "IQMStack",
    "QBackendIQMFactory",
    "compatible_stacks",
    "installed_stack",
    "required_stack",
]
