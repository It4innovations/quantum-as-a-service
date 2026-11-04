"""Adapter for the IQM 36 stack (``qaas[iqm_qiskit]``).

iqm-client 36.x, iqm-station-control-client 14.x, iqm-pulla 15.x.
Import this module only when that stack is installed; use
:class:`~qheappe_client.iqm_compat.QBackendIQMFactory` to pick it.

Changes from 35.x that matter here:

* ``RunRequest`` -> ``CircuitJobDefinition`` and
  ``IQMBackend.create_run_request`` -> ``create_circuit_job_definition``
  (handled in :class:`~qheappe_client.qiskit.qbackend_iqm.QBackendIQM`).
* iqm-pulla 15 no longer ships ``iqm.pulla`` (no ``Pulla`` / ``PullaJob``);
  pulse-level helpers moved to ``iqm.qiskit_iqm.pulse_level_utils``.
* ``JobStatus`` moved to ``iqm.station_control.interface.models.jobs_new``.
* Constructing ``ObservationLite`` directly emits a DeprecationWarning.
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar

from iqm.cpc.compiler.standard_stages import HERALDING_KEY
from iqm.qiskit_iqm.pulse_level_utils import qiskit_to_iqm, sweep_job_to_qiskit
from iqm.station_control.client.qon import ObservationFinder
from iqm.station_control.interface.models import CircuitJobDefinition
from iqm.station_control.interface.models.jobs_new import JobStatus

from ..qiskit.qbackend_iqm import QBackendIQM
from .stack import IQMStack

__all__ = [
    "HERALDING_KEY",
    "STACK",
    "CircuitJobDefinition",
    "JobStatus",
    "QBackendIQM36",
    "qiskit_to_iqm",
    "qiskit_to_pulla",
    "sweep_job_to_qiskit",
]

STACK = IQMStack.IQM36


class QBackendIQM36(QBackendIQM):
    """QBackendIQM for iqm-client 36.x."""

    iqm_stack: ClassVar[IQMStack] = STACK

    @staticmethod
    def _load_metrics(metrics: dict[str, Any]) -> ObservationFinder:
        """Same as the base, without the 36.x ObservationLite deprecation warning.

        The metrics endpoint returns lite observations (no dut_label/source/tags),
        so ObservationLite is still the right model for them.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return QBackendIQM._load_metrics(metrics)


def qiskit_to_pulla(*_args, **_kwargs):
    """Not available with iqm-client 36.x.

    Raises:
        NotImplementedError: Always. iqm-pulla 15 removed the ``Pulla`` class.
    """
    raise NotImplementedError(
        "qiskit_to_pulla needs iqm-pulla 14.x (install 'qaas[iqm35_qiskit]'). "
        "With iqm-client 36.x use iqm.qiskit_iqm.pulse_level_utils.qiskit_to_iqm "
        "(re-exported here as qiskit_to_iqm)."
    )
