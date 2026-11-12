"""Qiskit ``SamplerV2`` primitive for QaaS backends.

:class:`QSamplerV2` runs sampler PUBs on a :class:`~qheappe_client.qiskit.qbackend.QBackend`
and returns a ``PrimitiveResult`` with one ``BitArray`` per classical register, as
``qiskit.primitives.BackendSamplerV2`` does. Results are read from the IQM measurement
keys, so the bit order follows the Qiskit convention for any register layout.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import numpy as np
from qiskit import QuantumCircuit
from qiskit.primitives import BaseSamplerV2, PrimitiveResult, SamplerPubResult
from qiskit.primitives.base.base_primitive_job import BasePrimitiveJob
from qiskit.primitives.containers import BitArray, DataBin
from qiskit.primitives.containers.sampler_pub import SamplerPub, SamplerPubLike
from qiskit.providers.jobstatus import JOB_FINAL_STATES, JobStatus

if TYPE_CHECKING:
    from .qbackend import QBackend, QJob

__all__ = ["QSamplerJob", "QSamplerV2"]

# IQM measurement key: "<creg name>_<creg size>_<creg index>_<clbit index>"
# (see iqm.qiskit_iqm.qiskit_to_iqm.MeasurementKey).
_MEAS_KEY = re.compile(r"^(.*)_(\d+)_(\d+)_(\d+)$")

# A sampler job reports the "worst" status of its backend jobs, in this order.
_STATUS_PRIORITY = (
    JobStatus.ERROR,
    JobStatus.CANCELLED,
    JobStatus.RUNNING,
    JobStatus.VALIDATING,
    JobStatus.QUEUED,
    JobStatus.INITIALIZING,
    JobStatus.DONE,
)


class QSamplerV2(BaseSamplerV2):
    """Sampler primitive (``SamplerV2``) that runs on a QaaS backend.

    Each call to :meth:`run` binds the parameters of every PUB, submits all the
    resulting circuits as one backend job per distinct shot count, and returns a
    :class:`QSamplerJob` whose result is a ``PrimitiveResult``.

    Circuits must already be transpiled for the backend (``backend.transpile(qc)``).

    Example:
        >>> sampler = QSamplerV2(backend)
        >>> job = sampler.run([backend.transpile(qc)], shots=1000)
        >>> counts = job.result()[0].data.meas.get_counts()
    """

    def __init__(
        self,
        backend: QBackend,
        *,
        default_shots: int | None = None,
        run_options: dict[str, Any] | None = None,
    ):
        """Create a sampler for ``backend``.

        Args:
            backend (QBackend): The QaaS backend to run on.
            default_shots (int | None): Shots for PUBs that set none; the backend's
                ``shots`` option if ``None``.
            run_options (dict[str, Any] | None): Extra options passed to ``backend.run()``,
                e.g. ``job_name`` or ``circuit_compilation_options``.
        """
        self._backend = backend
        self._default_shots = default_shots
        self._run_options = dict(run_options or {})

    @property
    def backend(self) -> QBackend:
        """The backend this sampler runs on."""
        return self._backend

    @property
    def default_shots(self) -> int:
        """Shots used for PUBs that set none."""
        if self._default_shots is not None:
            return self._default_shots
        return int(self._backend.options.shots)

    def run(
        self, pubs: Iterable[SamplerPubLike], *, shots: int | None = None
    ) -> QSamplerJob:
        """Submit PUBs and return a job for their results.

        Args:
            pubs (Iterable[SamplerPubLike]): Circuits, or ``(circuit, parameter_values[, shots])`` tuples.
            shots (int | None): Shots for PUBs that set none; :attr:`default_shots` if ``None``.

        Returns:
            QSamplerJob: Job whose :meth:`~QSamplerJob.result` is a ``PrimitiveResult``.

        Raises:
            ValueError: If a PUB circuit has no classical registers.
        """
        if shots is None:
            shots = self.default_shots
        coerced = [SamplerPub.coerce(pub, shots) for pub in pubs]
        for i, pub in enumerate(coerced):
            if not pub.circuit.cregs:
                raise ValueError(
                    f"PUB {i}: circuit has no classical registers, nothing to sample"
                )

        # One backend job per shot count (HEAppE takes one shot count per job).
        by_shots: dict[int, list[int]] = {}
        for i, pub in enumerate(coerced):
            by_shots.setdefault(pub.shots, []).append(i)

        groups = []
        for group_shots, indices in by_shots.items():
            bound = {
                i: coerced[i].parameter_values.bind_all(coerced[i].circuit)
                for i in indices
            }
            circuits: list[QuantumCircuit] = []
            for i in indices:
                circuits.extend(np.ravel(bound[i]).tolist())
            job = self._backend.run(
                circuits, shots=group_shots, memory=True, **self._run_options
            )
            groups.append((job, [(i, bound[i].shape) for i in indices]))

        return QSamplerJob(coerced, groups)


class QSamplerJob(BasePrimitiveJob[PrimitiveResult[SamplerPubResult], JobStatus]):
    """Job returned by :meth:`QSamplerV2.run`.

    Wraps one :class:`~qheappe_client.qiskit.qbackend.QJob` per distinct shot count;
    its id joins their HEAppE job ids with commas.
    """

    def __init__(
        self,
        pubs: list[SamplerPub],
        groups: list[tuple[QJob, list[tuple[int, tuple[int, ...]]]]],
    ):
        """Create the job.

        Args:
            pubs (list[SamplerPub]): The coerced PUBs, in submission order.
            groups (list): ``(backend job, [(pub index, bound shape), ...])`` per backend job.
        """
        super().__init__(",".join(job.job_id() for job, _ in groups))
        self._pubs = pubs
        self._groups = groups
        self._result: PrimitiveResult[SamplerPubResult] | None = None

    @property
    def jobs(self) -> list[QJob]:
        """The backend jobs behind this sampler job."""
        return [job for job, _ in self._groups]

    def result(self) -> PrimitiveResult[SamplerPubResult]:
        """Wait for the backend jobs and return one ``SamplerPubResult`` per PUB.

        Returns:
            PrimitiveResult[SamplerPubResult]: Per PUB, a ``DataBin`` with one ``BitArray``
            per classical register of the circuit, shaped like the PUB.

        Raises:
            RuntimeError: If a backend job fails or is canceled.
            ValueError: If the measurement data does not match the circuits.
        """
        if self._result is not None:
            return self._result

        pub_results: list[SamplerPubResult | None] = [None] * len(self._pubs)
        for job, entries in self._groups:
            measurements = job._finished_artifacts(with_measurements=True)[
                job._MEAS_ARTIFACT
            ]
            expected = sum(int(np.prod(shape, dtype=int)) for _, shape in entries)
            if len(measurements) != expected:
                raise ValueError(
                    f"Job {job.job_id()}: {len(measurements)} measurement sets for "
                    f"{expected} circuits"
                )
            start = 0
            for i, shape in entries:
                size = int(np.prod(shape, dtype=int))
                pub_results[i] = _pub_result(
                    self._pubs[i], shape, measurements[start : start + size], job
                )
                start += size

        self._result = PrimitiveResult(pub_results, metadata={"version": 2})
        return self._result

    def status(self) -> JobStatus:
        """Combined status of the backend jobs (the least advanced one wins)."""
        statuses = {job.status() for job in self.jobs}
        return next(s for s in _STATUS_PRIORITY if s in statuses)

    def done(self) -> bool:
        """Whether all backend jobs finished successfully."""
        return self.status() == JobStatus.DONE

    def running(self) -> bool:
        """Whether any backend job is running."""
        return self.status() == JobStatus.RUNNING

    def cancelled(self) -> bool:
        """Whether a backend job was canceled."""
        return self.status() == JobStatus.CANCELLED

    def in_final_state(self) -> bool:
        """Whether the job is done, failed or canceled."""
        return self.status() in JOB_FINAL_STATES

    def wait_for_final_state(
        self,
        timeout: float | None = None,
        wait: float = 5,
        callback: Callable | None = None,
    ) -> None:
        """Wait until every backend job reaches a final state.

        Follows the HEAppE event websocket of each backend job (see
        :meth:`QJob.wait_for_final_state`), one job after another, sharing ``timeout``.

        Args:
            timeout (float | None): Seconds to wait in total. If ``None``, wait
                indefinitely.
            wait (float): Seconds between backup job-info queries.
            callback (Callable | None): Called as ``callback(job_id, job_status, job)``
                with this sampler job's id, combined status and the job itself,
                whenever a backend job's status changes.

        Raises:
            JobTimeoutError: If the jobs do not reach a final state before the timeout.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def report(_job_id, _status, _job):
            callback(self.job_id(), self.status(), self)

        for job in self.jobs:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            job.wait_for_final_state(
                timeout=remaining, wait=wait, callback=report if callback else None
            )

    def cancel(self) -> None:
        """Cancel all backend jobs."""
        for job in self.jobs:
            job.cancel()


def _pub_result(
    pub: SamplerPub,
    shape: tuple[int, ...],
    measurements: list[dict[str, list[list[int]]]],
    job: QJob,
) -> SamplerPubResult:
    """Build the ``SamplerPubResult`` of one PUB from its per-circuit measurements.

    Args:
        pub (SamplerPub): The PUB.
        shape (tuple[int, ...]): Shape of the PUB's bound circuits.
        measurements (list): One ``{measurement key: [[bit], ...shots]}`` dict per bound circuit.
        job (QJob): Backend job that produced the data (for metadata and errors).

    Returns:
        SamplerPubResult
    """
    cregs = pub.circuit.cregs
    arrays = {
        creg.name: np.zeros((*shape, pub.shots, creg.size), dtype=bool)
        for creg in cregs
    }
    for index, circuit_meas in zip(np.ndindex(*shape), measurements, strict=True):
        for key, values in circuit_meas.items():
            creg_name, clbit = _creg_bit(key, cregs, job)
            bits = np.asarray(values, dtype=bool)
            if bits.ndim != 2 or bits.shape[1] != 1:
                raise ValueError(
                    f"Job {job.job_id()}: measurement {key!r} has shape {bits.shape}, "
                    "expected (shots, 1)"
                )
            if bits.shape[0] != pub.shots:
                raise ValueError(
                    f"Job {job.job_id()}: measurement {key!r} has {bits.shape[0]} shots, "
                    f"expected {pub.shots}"
                )
            arrays[creg_name][(*index, slice(None), clbit)] = bits[:, 0]

    data = {
        name: BitArray.from_bool_array(array, order="little")
        for name, array in arrays.items()
    }
    return SamplerPubResult(
        DataBin(**data, shape=shape),
        metadata={
            "shots": pub.shots,
            "circuit_metadata": pub.circuit.metadata,
            "job_id": job.job_id(),
        },
    )


def _creg_bit(key: str, cregs: list, job: QJob) -> tuple[str, int]:
    """Map an IQM measurement key to ``(register name, bit index in register)``.

    Args:
        key (str): IQM measurement key, ``<creg>_<size>_<creg index>_<clbit index>``.
        cregs (list): Classical registers of the PUB circuit.
        job (QJob): Backend job (for errors).

    Returns:
        tuple[str, int]

    Raises:
        ValueError: The key is malformed or names no register of the circuit.
    """
    match = _MEAS_KEY.match(key)
    if match is None:
        raise ValueError(f"Job {job.job_id()}: unexpected measurement key {key!r}")
    name, _size, creg_idx, clbit = match.group(1), *map(int, match.groups()[1:])
    by_name = {creg.name: creg for creg in cregs}
    creg = by_name.get(name)
    if creg is None and creg_idx < len(cregs):
        creg = cregs[creg_idx]
    if creg is None or clbit >= creg.size:
        raise ValueError(
            f"Job {job.job_id()}: measurement key {key!r} does not match the circuit's "
            f"registers {[c.name for c in cregs]}"
        )
    return creg.name, clbit
