import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import aclosing
from typing import Any

from qiskit import QuantumCircuit
from qiskit.primitives import PrimitiveResult
from qiskit.providers import BackendV2, JobV1
from qiskit.providers.exceptions import JobTimeoutError
from qiskit.providers.jobstatus import JOB_FINAL_STATES, JobStatus
from qiskit.providers.options import Options
from qiskit.result import Result

from ..qclient import QClient
from ..utils import HEAppEJobState

log = logging.getLogger(__name__)

HEAppE_QISKIT_STATUS_MAPPING = {
    "FINISHED": JobStatus.DONE,
    "WAITING": JobStatus.RUNNING,
    "RUNNING": JobStatus.RUNNING,
    "SUBMITTED": JobStatus.QUEUED,
    "QUEUED": JobStatus.QUEUED,
    "WAITINGFORSERVICEACCOUNT": JobStatus.QUEUED,
    "CONFIGURING": JobStatus.INITIALIZING,
    "FAILED": JobStatus.ERROR,
    "CANCELLED": JobStatus.CANCELLED,
    "CANCELED": JobStatus.CANCELLED,
    "UNKNOWN": JobStatus.ERROR,
}


class _HEAppEJobId(int):
    """HEAppE job id (int). Also callable, so job.job_id() returns the Qiskit-style string."""

    def __call__(self) -> str:
        """Returns the Qiskit-style string representation of the job ID.

        Returns:
            str: The string representation of the job ID.
        """
        return str(int(self))


class QBackend(BackendV2, ABC):
    """Abstract QaaS backend backed by QClient. Subclasses must implement run() returning QJob.

    Deliberately does not define `target` or `max_circuits`, so a vendor base class
    placed after it in the MRO (e.g. IQMBackendBase) can supply them.
    """

    def __init__(self, client: QClient, **kwargs):
        """Initialize the QBackend with a QClient.

        Args:
            client (QClient): The QClient instance to use for communication with the backend.
            **kwargs: Additional keyword arguments to be passed to the BackendV2 constructor.
        """
        # Cooperative init: forwards remaining kwargs along the MRO (vendor base -> BackendV2).
        # TODO: finish readable kwargs for BackendV2 from QISKIT, e.g. provider, name, description, version, url, etc.
        BackendV2.__init__(self)
        self._qclient = client

    @classmethod
    def _default_options(cls) -> Options:
        """Return the default options for the backend.

        Returns:
            Options: The default options: ``shots=1024`` and ``memory=False``.
        """
        return Options(shots=1024, memory=False)

    @abstractmethod
    def run(
        self,
        run_input: QuantumCircuit | list[QuantumCircuit],
        shots: int | None = None,
        **options,
    ) -> "QJob":
        """Serialize circuit(s) and submit them; must return a QJob.

        Args:
            run_input (QuantumCircuit | list[QuantumCircuit]): The quantum circuit(s) to run.
            shots (int | None): The number of shots to execute; the backend's ``shots``
                option (``backend.options.shots``) if ``None``, as ``BackendV2.run()`` expects.
            **options: Additional options for the run.

        Returns:
            QJob: The QJob instance representing the submitted job.
        """

    @abstractmethod
    def result_to_qiskit(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
    ) -> PrimitiveResult:
        """Convert raw results and measurements to a Qiskit PrimitiveResult.

        Args:
            raw (list[dict]): List of dictionaries containing raw results from the quantum backend.
            measurements_raw (list[dict] | None): Optional list of dictionaries containing raw measurements.
            reg_name (str): Name of the register to use for measurements.

        Returns:
            PrimitiveResult: A Qiskit PrimitiveResult object.
        """

    @abstractmethod
    def result_to_qiskit_legacy(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
        backend_name: str = "my_backend",
        job_id: str = "",
        circuit_headers: list[dict[str, Any]] | None = None,
    ) -> Result:
        """Convert raw results to a Qiskit Result object in legacy format.

        Args:
            raw (list[dict]): A list of dictionaries containing measurement keys and counts.
                Each dictionary should have the form:
                {"measurement_keys": [...], "counts": {...}}
            measurements_raw (list[dict] | None): Optional, one dictionary per PUB containing raw measurements.
                If provided, used instead of counts to preserve shot order.
                Format: {key: [[bit, ...], ...shots]}
            reg_name (str): The name of the classical register to use for measurement results.
            backend_name (str): The name of the backend used for the execution.
            job_id (str): The ID of the job that produced the results.
            circuit_headers (list[dict[str, Any]] | None): Optional, one
                ``{"name": ..., "metadata": ...}`` per circuit for the experiment headers.

        Returns:
            Result: A Result object containing the processed measurement results.

        Raises:
            ValueError: If the number of raw results does not match the number of measurement sets.
        """

    def _submit(
        self,
        circuit_payloads: dict[str, str],
        shots: int,
        circuit_headers: list[dict[str, Any]] | None = None,
        **options,
    ) -> "QJob":
        """Builds HEAppE tasks from {part_name: payload} and submits them via QClient.

        Args:
            circuit_payloads (dict[str, str]): A dictionary mapping part names to circuit payloads.
            shots (int): The number of shots to execute.
            circuit_headers (list[dict[str, Any]] | None): ``{"name", "metadata"}`` of each
                submitted circuit, put into the result headers.
            **options: Additional options for the submission, such as walltime_limit_secs, use_sessions
                and memory (if True, the job's results include per-shot measurements by default).

        Returns:
            QJob: The QJob instance representing the submitted job.
        """
        machine_id = self._qclient.get_quantum_backend_info().backend_name

        tasks = [
            {
                "Name": f"qheappe_task_{i}",
                "MachineId": machine_id,
                "WalltimeLimitSecs": options.get("walltime_limit_secs", 3600),
                "PayloadPartName": part_name,
                "UseSessions": options.get("use_sessions", False),
                "EnvironmentVariables": {"RUN_SHOTS": str(shots)},
            }
            for i, part_name in enumerate(circuit_payloads)
        ]

        submitted_job = self._qclient._run(
            self._qclient.submit_quantum_job(
                tasks=tasks,
                job_name=options.get("job_name", "qiskit_qasm_job"),
                circuit_payloads=circuit_payloads,
            )
        )
        heappe_job_id = (
            submitted_job.get("Id")
            if isinstance(submitted_job, dict)
            else submitted_job
        )

        return QJob(
            backend=self,
            heappe_job_id=heappe_job_id,
            submitted_job=submitted_job,
            shots=shots,
            memory=bool(options.get("memory", self.options.memory)),
            circuit_headers=circuit_headers,
        )

    @abstractmethod
    def transpile(
        self, circuits: list[QuantumCircuit] | QuantumCircuit, **kwargs
    ) -> list[QuantumCircuit] | QuantumCircuit:
        """Transpile circuits to target machine.

        Args:
            circuits (list[QuantumCircuit] | QuantumCircuit): The quantum circuit(s) to transpile.
            **kwargs: Additional keyword arguments for the transpiler.

        Returns:
            list[QuantumCircuit] | QuantumCircuit: The transpiled quantum circuit(s).
        """


class QJob(JobV1):
    """QaaS Job class inheriting from Qiskit's JobV1."""

    _COUNTS_ARTIFACT = "measurement_counts"
    _MEAS_ARTIFACT = "measurements"

    def __init__(
        self,
        backend: QBackend,
        heappe_job_id: int,
        submitted_job: dict[str, Any] | None = None,
        job_type: str = "circuit",
        shots: int | None = None,
        memory: bool = False,
        circuit_headers: list[dict[str, Any]] | None = None,
    ):
        """Initialize the QJob.

        Args:
            backend (QBackend): The backend associated with this job.
            heappe_job_id (int): The HEAppE job ID.
            submitted_job (dict[str, Any] | None): The submitted job details.
            job_type (str): The type of job, default is "circuit".
            shots (int | None): The number of shots for the job.
            memory (bool): The ``memory`` option passed to ``backend.run()``. If True,
                :meth:`result` includes per-shot results.
            circuit_headers (list[dict[str, Any]] | None): ``{"name", "metadata"}`` of each
                circuit, set by ``backend.run()``. Without it (a job re-created from its
                id), result experiments are named ``circuit-<i>`` with empty metadata.
        """
        super().__init__(backend, str(heappe_job_id))
        self.job_id = _HEAppEJobId(heappe_job_id)
        self._backend: QBackend = backend
        self._qclient: QClient = backend._qclient
        self._submitted_job = submitted_job or {"Id": heappe_job_id}
        self._shots = shots if shots is not None else backend.options.shots
        self._memory = memory
        self.remote_job = None
        self._cancel_requested = False
        self._last_known_status: str = "SUBMITTED"
        self._cached_raw_results = None
        self._artifacts: dict[str, Any] | None = None
        self._artifacts_have_measurements = False
        self._result: Result | None = None
        self._circuit_headers = circuit_headers

    def status(self) -> JobStatus:
        """Queries HEAppE job status via QClient and maps it to Qiskit JobStatus.

        HEAppE is queried on every call until the job reaches a terminal state.

        Returns:
            JobStatus: The current status of the job.
        """
        if self.remote_job:
            return self.remote_job.status()

        if self._cancel_requested:
            return JobStatus.CANCELLED

        if self._last_known_status.upper() not in self._qclient.TERMINAL_JOB_STATES:
            self._refresh_job_info()

        return self._to_qiskit_status(self._last_known_status.upper())

    def _to_qiskit_status(self, heappe_status: str) -> JobStatus:
        """Maps a HEAppE status string to Qiskit JobStatus, handling explicit cancellations.

        Args:
            heappe_status (str): The HEAppE status string.

        Returns:
            JobStatus: The corresponding Qiskit JobStatus.
        """
        status = HEAppE_QISKIT_STATUS_MAPPING.get(heappe_status, JobStatus.ERROR)
        if self._cancel_requested and status is JobStatus.ERROR:
            return JobStatus.CANCELLED
        return status

    def _get_artifacts(self, with_measurements: bool) -> dict[str, Any]:
        """Download artifacts once; re-download only if measurements are newly requested.

        Args:
            with_measurements (bool): If True, download measurements as well.

        Returns:
            dict[str, Any]: The downloaded artifacts.
        """
        if self._artifacts is not None and (
            self._artifacts_have_measurements or not with_measurements
        ):
            return self._artifacts

        names = [QJob._COUNTS_ARTIFACT] + (
            [QJob._MEAS_ARTIFACT] if with_measurements else []
        )
        self._cached_raw_results = self._qclient.get_job_results(
            self._submitted_job, artifact_names=names
        )

        self._artifacts = self._cached_raw_results[self.job_id].get("artifacts", {})
        self._artifacts_have_measurements = with_measurements
        return self._artifacts

    def result(self) -> Result:
        """Wait for the job and return its result as a :class:`qiskit.result.Result`.

        Returns a ``Result``, as ``JobV1.result()`` requires, so tools that take a
        backend (``BackendSamplerV2``, ``BackendEstimatorV2``, ``qiskit-experiments``)
        work unchanged. For ``PrimitiveResult`` output, use :class:`QSamplerV2`.

        Per-shot results (``res.get_memory()``) are included when the job was submitted
        with ``backend.run(..., memory=True)`` (or created with ``QJob(..., memory=True)``).
        Bitstrings follow the Qiskit order: registers in circuit order, clbit 0 rightmost.

        Returns:
            Result: The result of the circuit execution:
            ``res.get_counts()`` (counts), ``res.get_memory()`` (per-shot results).

        Raises:
            RuntimeError: If the job fails or is canceled.
            ValueError: If the number of raw results does not match the number of measurement sets.
        """
        if self._result is not None:
            return self._result

        artifacts = self._finished_artifacts(self._memory)
        self._result = self._backend.result_to_qiskit_legacy(
            artifacts.get(QJob._COUNTS_ARTIFACT, []),
            measurements_raw=artifacts.get(QJob._MEAS_ARTIFACT)
            if self._memory
            else None,
            backend_name=self._backend.name,
            job_id=self._job_id,  # plain str stored by JobV1
            circuit_headers=self._circuit_headers,
        )
        return self._result

    def _finished_artifacts(self, with_measurements: bool) -> dict[str, Any]:
        """Wait until the job finishes, then return its downloaded artifacts.

        Args:
            with_measurements (bool): Also download per-shot measurements.

        Returns:
            dict[str, Any]: Artifacts by name (``measurement_counts``, ``measurements``).

        Raises:
            RuntimeError: If the job fails, is canceled, or returned no per-shot
                measurements when they were requested.
        """
        try:
            final_status = self._wait_for_terminal_state()
        except Exception as e:
            if self._cancel_requested:
                raise RuntimeError(f"Job {self.job_id()} was canceled.") from e
            raise RuntimeError(f"Job {self.job_id()} failed: {e!s}") from e

        if final_status.upper() not in ["FINISHED", "DONE"]:
            if self._cancel_requested or final_status.upper() == "CANCELED":
                raise RuntimeError(f"Job {self.job_id()} was canceled.")
            raise RuntimeError(
                f"Job {self.job_id()} failed with status: {final_status}"
            )

        artifacts = self._get_artifacts(with_measurements=with_measurements)
        if with_measurements and artifacts.get(QJob._MEAS_ARTIFACT) is None:
            raise RuntimeError(
                f"Job {self.job_id()}: backend returned no per-shot measurements."
            )
        return artifacts

    def _refresh_job_info(self) -> str:
        """Fetch the current job info from HEAppE and store it.

        If HEAppE returns no ``State``, the last known state is kept.

        Returns:
            str: The job state, e.g. ``"RUNNING"`` or ``"FINISHED"``.
        """
        info = self._qclient.get_job_info(self.job_id())
        if info.get("State") is not None:
            self._submitted_job = info
            self._last_known_status = HEAppEJobState.readable(info["State"])
        return self._last_known_status

    def _wait_for_terminal_state(self) -> str:
        """Return the job's terminal state, waiting for it if needed.

        Uses a terminal state already seen (by :meth:`status` or an earlier wait).
        Otherwise asks HEAppE first, so a job that already finished is not waited on:
        the event stream does not report state changes that happened before it opened.
        After waiting, the job info is fetched again, so ``_submitted_job`` holds the
        final ``State`` and ``Tasks``.

        Returns:
            str: Terminal state, e.g. ``"FINISHED"``.
        """
        terminal = self._qclient.TERMINAL_JOB_STATES
        if self._last_known_status.upper() in terminal:
            return self._last_known_status
        self.wait_for_final_state()
        final = self._last_known_status
        if self._refresh_job_info().upper() not in terminal:
            # Job info can lag behind the event stream (still SUBMITTED for a few
            # seconds after the job finished), so keep the final state already seen.
            self._last_known_status = final
        return self._last_known_status

    def wait_for_final_state(
        self,
        timeout: float | None = None,
        wait: float = 5,
        callback: Callable | None = None,
    ) -> None:
        """Wait until the job reaches a final state such as ``DONE`` or ``ERROR``.

        Same interface as ``JobV1.wait_for_final_state``, but follows the HEAppE event
        websocket instead of polling. Job info is still fetched every ``wait`` seconds
        as a backup, because the websocket only reports state changes that happen
        after it connects (and may close early).

        Args:
            timeout (float | None): Seconds to wait for the job. If ``None``, wait
                indefinitely.
            wait (float): Seconds between backup job-info queries.
            callback (Callable | None): Called as ``callback(job_id, job_status, job)``
                with the current status before waiting and on each status change
                that is not final.

        Raises:
            JobTimeoutError: If the job does not reach a final state before the
                specified timeout.
        """
        if self.remote_job:
            self.remote_job.wait_for_final_state(timeout=timeout, callback=callback)
            return
        status = self.status()
        if status in JOB_FINAL_STATES:
            return
        if callback:
            callback(self.job_id(), status, self)
        try:
            self._qclient._run(self._await_final_state(timeout, wait, callback, status))
        except TimeoutError:
            raise JobTimeoutError(
                f"Timeout while waiting for job {self.job_id()}."
            ) from None

    async def _await_final_state(
        self,
        timeout: float | None,
        wait: float,
        callback: Callable | None,
        status: JobStatus,
    ) -> None:
        """Follow the event websocket, with a job-info query every ``wait`` seconds.

        Args:
            timeout (float | None): Maximum wait in seconds; unlimited if ``None``.
            wait (float): Seconds between backup job-info queries.
            callback (Callable | None): See :meth:`wait_for_final_state`.
            status (JobStatus): The last status already reported to ``callback``.

        Raises:
            TimeoutError: The job did not reach a final state in time.
        """
        last = [status]

        def update(state: str) -> bool:
            """Record a HEAppE state; True once it is final."""
            self._last_known_status = state.upper()
            new = self._to_qiskit_status(self._last_known_status)
            if new in JOB_FINAL_STATES:
                return True
            if new != last[0]:
                last[0] = new
                if callback:
                    callback(self.job_id(), new, self)
            return False

        async def from_events() -> None:
            try:
                async with aclosing(self._qclient.iter_events()) as events:
                    async for event in events:
                        data = event.get("data") or {}
                        job_id, state = data.get("jobId"), data.get("state")
                        if job_id is None or int(job_id) != int(self.job_id):
                            continue
                        if state and update(str(state)):
                            return
            except Exception as e:  # noqa: BLE001 - fall back to job-info queries
                log.debug("Event stream for job %s failed: %s", self.job_id, e)
            await asyncio.Event().wait()  # stream gone: rely on job-info queries

        async def from_job_info() -> None:
            while True:
                await asyncio.sleep(wait)
                state = (await self._qclient.aget_job_info(int(self.job_id))).get(
                    "State"
                )
                if state is not None and update(HEAppEJobState.readable(state)):
                    return

        tasks = [
            asyncio.create_task(from_events()),
            asyncio.create_task(from_job_info()),
        ]
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # surface errors from the finished source
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def submit(self) -> None:
        """Submission is performed directly inside QBackend.run()."""

    def cancel(self) -> bool:
        """Ask HEAppE to cancel the job.

        Returns:
            bool: ``True`` if HEAppE accepted the request; :meth:`status` then reports
            ``JobStatus.CANCELLED``. ``False`` if it was refused (the error is logged).
        """
        accepted = bool(self._qclient._run(self._qclient.cancel_job(int(self.job_id))))
        if accepted:
            self._cancel_requested = True
        return accepted
