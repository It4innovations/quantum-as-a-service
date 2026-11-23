"""Adapter for the IQM 35 stack (``qaas[iqm35_qiskit]``).

iqm-client 35.x, iqm-station-control-client 13.x, iqm-pulla 14.x.
Import this module only when that stack is installed; use
:class:`~qheappe_client.iqm_compat.QBackendIQMFactory` to pick it.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import ClassVar

from iqm.iqm_server_client.models import JobStatus
from iqm.pulla.interface import HERALDING_KEY
from iqm.pulla.pulla import PullaJob
from iqm.pulla.utils_qiskit import qiskit_to_pulla as _iqm_qiskit_to_pulla
from iqm.qiskit_iqm import IQMJob
from iqm.station_control.interface.models import RunRequest
from qiskit.result import Counts, Result

from ..qiskit.qbackend_iqm import QBackendIQM
from .stack import IQMStack

__all__ = [
    "HERALDING_KEY",
    "STACK",
    "JobStatus",
    "QBackendIQM35",
    "RunRequest",
    "qiskit_to_pulla",
    "sweep_job_to_qiskit",
]

STACK = IQMStack.IQM35


class QBackendIQM35(QBackendIQM):
    """QBackendIQM for iqm-client 35.x."""

    iqm_stack: ClassVar[IQMStack] = STACK


def qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits):
    """Delegate to ``iqm.pulla.utils_qiskit.qiskit_to_pulla``.

    Args:
        pulla: Pulla instance.
        pulla_backend: Pulla backend.
        qiskit_circuits: Qiskit circuits.

    Returns:
        Result of the conversion.
    """
    return _iqm_qiskit_to_pulla(pulla, pulla_backend, qiskit_circuits)


def sweep_job_to_qiskit(
    job: PullaJob,
    *,
    shots: int,
) -> Result:
    """Convert a completed Pulla job to a Qiskit Result.

    Patched copy of ``iqm.pulla.utils_qiskit.sweep_job_to_qiskit`` (35.x has a bug in its datetime import).

    Args:
        job: The completed job to convert.
        shots: Number of shots that was requested. Only used for validating the result.

    Returns:
        The equivalent Qiskit Result.

    Raises:
        ValueError: If the job result or circuit measurement results are missing.
    """
    circuit_execution_results = job.result()
    if circuit_execution_results is None:
        raise ValueError(
            f'Cannot format Qiskit result without result measurements. Job status is "{job.status.upper()}"'
        )

    if circuit_execution_results.circuit_measurement_results is None:
        raise ValueError("Cannot format station control result without result.")

    used_heralding = (
        sum(HERALDING_KEY in key for key in circuit_execution_results.sweep_results) > 0
    )

    # Convert the measurement results from a batch of circuits into the Qiskit format.
    batch_results: list[tuple[str, list[str]]] = [
        # TODO: Proper naming instead of "index"
        (
            f"{index:04d}",
            IQMJob._iqm_format_measurement_results(
                circuit_measurements,
                requested_shots=shots,
                expect_exact_shots=used_heralding,
            ),
        )
        for index, circuit_measurements in enumerate(
            circuit_execution_results.circuit_measurement_results
        )
    ]

    result_dict = {
        "backend_name": "IQMPullaBackend",
        "backend_version": "",
        "qobj_id": "",
        "job_id": str(job.job_id),
        "success": job.status == JobStatus.COMPLETED,
        "date": datetime.now(tz=UTC).date().isoformat(),
        "status": str(job.status),
        "timeline": job.data.timeline.copy(),
        "results": [
            {
                "shots": len(measurement_results),
                "success": True,
                "data": {
                    "memory": measurement_results,
                    "counts": Counts(Counter(measurement_results)),
                    "metadata": {},
                },
                "header": {"name": name},
            }
            for name, measurement_results in batch_results
        ],
    }
    return Result.from_dict(result_dict)
