from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from qiskit import QuantumCircuit
from qiskit.primitives import PrimitiveResult
from qiskit.providers import BackendV2, JobV1
from qiskit.providers.jobstatus import JobStatus
from qiskit.providers.options import Options
from qiskit.result import Result

from ..qclient import QClient
from ..utils import HEAppEJobState

HEAppE_QISKIT_STATUS_MAPPING = {
    "FINISHED": JobStatus.DONE,
    "WAITING": JobStatus.RUNNING,
    "RUNNING": JobStatus.RUNNING,
    "SUBMITTED": JobStatus.QUEUED,
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
            Options: The default options with shots set to 1024.
        """
        return Options(shots=1024)

    @abstractmethod
    def run(
        self, run_input: QuantumCircuit | list[QuantumCircuit], shots: int, **options
    ) -> "QJob":
        """Serialize circuit(s) and submit them; must return a QJob.

        Args:
            run_input (QuantumCircuit | list[QuantumCircuit]): The quantum circuit(s) to run.
            shots (int): The number of shots to execute.
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
        key_order_left_to_right: bool = True,
    ) -> PrimitiveResult:
        """Convert raw results and measurements to a Qiskit PrimitiveResult.

        Args:
            raw (list[dict]): List of dictionaries containing raw results from the quantum backend.
            measurements_raw (list[dict] | None): Optional list of dictionaries containing raw measurements.
            reg_name (str): Name of the register to use for measurements.
            key_order_left_to_right (bool): If True, the bit order of the measurement results is from left to right.
                If False, the bit order is from right to left.

        Returns:
            PrimitiveResult: A Qiskit PrimitiveResult object.
        """

    @abstractmethod
    def result_to_qiskit_legacy(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
        key_order_left_to_right: bool = True,
        backend_name: str = "my_backend",
        job_id: str = "",
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
            key_order_left_to_right (bool): If True, measurement_keys[0] corresponds to clbit 0.
            backend_name (str): The name of the backend used for the execution.
            job_id (str): The ID of the job that produced the results.

        Returns:
            Result: A Result object containing the processed measurement results.

        Raises:
            ValueError: If the number of raw results does not match the number of measurement sets.
        """

    def _submit(
        self, circuit_payloads: dict[str, str], shots: int, **options
    ) -> "QJob":
        """Builds HEAppE tasks from {part_name: payload} and submits them via QClient.

        Args:
            circuit_payloads (dict[str, str]): A dictionary mapping part names to circuit payloads.
            shots (int): The number of shots to execute.
            **options: Additional options for the submission, such as walltime_limit_secs and use_sessions.

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
    ):
        """Initialize the QJob.

        Args:
            backend (QBackend): The backend associated with this job.
            heappe_job_id (int): The HEAppE job ID.
            submitted_job (dict[str, Any] | None): The submitted job details.
            job_type (str): The type of job, default is "circuit".
            shots (int | None): The number of shots for the job.
        """
        super().__init__(backend, str(heappe_job_id))
        self.job_id = _HEAppEJobId(heappe_job_id)
        self._backend: QBackend = backend
        self._qclient: QClient = backend._qclient
        self._submitted_job = submitted_job or {"Id": heappe_job_id}
        self._shots = shots if shots is not None else backend.options.shots
        self.remote_job = None
        self._cancel_requested = False
        self._last_known_status: str = "SUBMITTED"
        self._cached_raw_results = None
        self._artifacts: dict[str, Any] | None = None
        self._artifacts_have_measurements = False
        self._result_cache: dict[tuple[bool, bool, bool], Result | PrimitiveResult] = {}

    def status(self) -> JobStatus:
        """Queries HEAppE job status via QClient and maps it to Qiskit JobStatus.

        Returns:
            JobStatus: The current status of the job.
        """
        if self.remote_job:
            return self.remote_job.status()

        if self._cancel_requested:
            return JobStatus.CANCELED

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
            return JobStatus.CANCELED
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
        # Any previously converted results remain valid; no need to clear _result_cache.
        return self._artifacts

    def result(
        self,
        with_measurements=False,
        key_order_left_to_right=False,
        qiskit_legacy=False,
    ) -> Result | PrimitiveResult:
        """Execute a quantum circuit on a quantum backend.

        Args:
            with_measurements (bool): If True, include particular shot measurements into the Qiskit Result.
                This allows accessing per-shot memory and counts directly from the result object.
            key_order_left_to_right (bool): If True, the bit order of the measurement results is from left to right.
                If False, the bit order is from right to left.
            qiskit_legacy (bool): Whether to use qiskit legacy result type or PrimitiveResult.

        Returns:
            Result | PrimitiveResult: The result of the circuit execution.

        Raises:
            RuntimeError: If the job fails or is canceled.
            ValueError: If the number of raw results does not match the number of measurement sets.

        Accessing results:
        - For PrimitiveResult:
            - res[0].data.meas.get_bitstrings() # per-shot memory
            - res[0].data.meas.get_counts() # should match the original counts (bit-reversed if key_order_left_to_right)
        - For Qiskit Legacy Result:
            - res.get_counts() # counts dictionary
            - res.get_memory() # per-shot memory list
        """
        # Return cached and formatted result
        key = (qiskit_legacy, with_measurements, key_order_left_to_right)
        if key in self._result_cache:
            return self._result_cache[key]

        # Otherwise wait for the job and fetch the results

        try:
            # Check the status in submitted job, if the info is not complete

            if "Tasks" not in self._submitted_job or "State" not in self._submitted_job:
                self._submitted_job = self._qclient.get_job_info(self.job_id())
            self._last_known_status = self._submitted_job.get("State", -1)

            if self._last_known_status == -1:
                raise RuntimeError("Unknown status of the job")
            final_status = self._last_known_status = HEAppEJobState.readable(self._last_known_status)

            if self._last_known_status not in self._qclient.TERMINAL_JOB_STATES:
                final_status = self._qclient.wait_for_job_terminal_state(self.job_id)
                self._last_known_status = final_status

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

        # Get artifacts of result

        artifacts = self._get_artifacts(with_measurements=with_measurements)

        counts = artifacts.get(QJob._COUNTS_ARTIFACT, [])
        measurements = artifacts.get(QJob._MEAS_ARTIFACT) if with_measurements else None
        if with_measurements and measurements is None:
            raise RuntimeError(
                f"Job {self.job_id()}: backend returned no per-shot measurements."
            )

        if qiskit_legacy:
            result = self._backend.result_to_qiskit_legacy(
                counts,
                measurements_raw=measurements,
                key_order_left_to_right=key_order_left_to_right,
                backend_name=self._backend.name,
                job_id=self._job_id,  # plain str stored by JobV1
            )
        else:
            result = self._backend.result_to_qiskit(
                counts,
                measurements_raw=measurements,
                key_order_left_to_right=key_order_left_to_right,
            )

        self._result_cache[key] = result
        return result

    def submit(self) -> None:
        """Submission is performed directly inside QBackend.run()."""

    def cancel(self) -> None:
        """Requests cancellation via QClient and flags local state."""
        self._cancel_requested = True
        self._qclient.cancel_job(self.job_id)
