"""IQM executor and storage that reach the quantum computer through HEAppE/QScheduler.

iqm-client 36.x builds :class:`~iqm.iqm_client.IQMClient` from two pluggable parts:

* an *executor* (:class:`~iqm.station_control.interface.executor_interface.ExecutorInterface`)
  that reads station artifacts and runs jobs, and
* a *storage* (:class:`~iqm.station_control.interface.storage_interface.StorageInterface`)
  that reads calibration data.

IQM's own implementations talk to an IQM Server. The ones here send the same requests
through HEAppE (6.7.0+) to QScheduler (v0.2.2+), so IQM's pulse-level workflow (compiler,
``submit_playlist``, job trackers, result post-processing) runs unchanged:

==================================  ==========================================================
IQM call                            HEAppE / QScheduler
==================================  ==========================================================
settings, channel properties,       ``/heappe/QScheduler/MachineArtifact``
chip design record, static arch.    -> ``GET /machine/{m}/artifacts/{name}``
calibration set                     ``/heappe/QScheduler/MachineCalibration`` (no endpoint)
dynamic architecture, metrics       ``/heappe/QScheduler/MachineCalibration`` (with endpoint)
submit ``RunDefinition``            ``CreateAndSubmitJob`` task with ``Kind="pulse"`` (protobuf)
submit ``CircuitJobDefinition``     ``CreateAndSubmitJob`` task with ``Kind="circuit"`` (JSON)
job status                          ``/heappe/JobManagement/CurrentInfoForJob``
sweep results, measurements         ``/heappe/QScheduler/GetTaskArtifact``
==================================  ==========================================================

Jobs are HEAppE jobs with one task. IQM identifies jobs by UUID, so the HEAppE job id
``n`` is exposed as ``UUID(int=n)`` (see :func:`job_uuid` / :func:`heappe_job_id`).

Only the read-only calibration queries are supported; writing observations, runs or
sequences raises :class:`NotImplementedError`.

Needs the IQM 36 stack (``qaas[iqm_qiskit]``).
"""

from __future__ import annotations

import abc
import copy
import json
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from exa.common.data.setting_node import SettingNode
from exa.common.errors.iqm_error import NotFoundError
from iqm.iqm_server_client.iqm_server_client import (
    CircuitCountsBatchAdapter,
    CircuitMeasurementResultsBatchAdapter,
)
from iqm.models.channel_properties import ChannelProperties
from iqm.station_control.client.job_trackers import CircuitJobTracker, RunJobTracker
from iqm.station_control.client.serializers import (
    deserialize_sweep_results,
    serialize_run_definition,
)
from iqm.station_control.client.serializers.channel_property_serializer import (
    unpack_channel_properties,
)
from iqm.station_control.client.serializers.setting_node_serializer import (
    deserialize_setting_node,
)
from iqm.station_control.interface.executor_interface import (
    ExecutorInterface,
    JobTracker,
)
from iqm.station_control.interface.models import (
    CircuitJobDefinition,
    CircuitMeasurementCounts,
    CircuitMeasurementResults,
    DynamicQuantumArchitecture,
    ObservationData,
    RunDefinition,
    SoftwareVersionSet,
    StaticQuantumArchitecture,
    StrUUID,
    SweepResults,
)
from iqm.station_control.interface.models.jobs_new import (
    JobData,
    JobDefinition,
    JobMessage,
    JobStatus,
    JobType,
    TimelineEntry,
)
from iqm.station_control.interface.models.observation_set import (
    CalibrationSet,
    QualityMetricSet,
)
from iqm.station_control.interface.models.type_aliases import StrUUIDOrDefault
from iqm.station_control.interface.storage_interface import StorageInterface

from ..qclient import (
    MachineArtifactIQM,
    MachineCalibrationIQMEndpoint,
    QSchedulerTaskKind,
)
from ..utils import HEAppEJobState, QException, to_utc

if TYPE_CHECKING:
    from ..qclient import QClient

log = logging.getLogger(__name__)

#: Source name used in job timeline entries and messages built from HEAppE job info.
HEAPPE_SOURCE = "heappe"

#: Task artifact holding the protobuf sweep results of a pulse task.
SWEEP_RESULTS_ARTIFACT = "sweep_results"
#: Task artifacts holding the results of a circuit task.
MEASUREMENTS_ARTIFACT = "measurements"
MEASUREMENT_COUNTS_ARTIFACT = "measurement_counts"

# HEAppE job state (HEAppEJobState name, upper case) -> IQM job status.
_HEAPPE_TO_IQM_STATUS: dict[str, JobStatus] = {
    "CONFIGURING": JobStatus.WAITING,
    "SUBMITTED": JobStatus.WAITING,
    "QUEUED": JobStatus.WAITING,
    "WAITINGFORSERVICEACCOUNT": JobStatus.WAITING,
    "RUNNING": JobStatus.PROCESSING,
    "FINISHED": JobStatus.COMPLETED,
    "FAILED": JobStatus.FAILED,
    "CANCELED": JobStatus.CANCELLED,
    "CANCELLED": JobStatus.CANCELLED,
}


def job_uuid(heappe_job_id: int) -> UUID:
    """IQM job id of a HEAppE job: ``UUID(int=heappe_job_id)``."""
    return UUID(int=int(heappe_job_id))


def heappe_job_id(job_id: StrUUID) -> int:
    """HEAppE job id of an IQM job id made by :func:`job_uuid`."""
    return (job_id if isinstance(job_id, UUID) else UUID(str(job_id))).int


def heappe_state_name(state: Any) -> str:
    """Upper-case HEAppE job state name from the numeric or textual ``State`` field."""
    if isinstance(state, str) and not state.isdigit():
        return state.upper()
    try:
        return HEAppEJobState.readable(int(state))
    except (TypeError, ValueError):
        return f"Unknown state {state}"


def iqm_job_status(state: Any) -> JobStatus:
    """IQM job status for a HEAppE job ``State``; unknown states count as waiting."""
    name = heappe_state_name(state)
    status = _HEAPPE_TO_IQM_STATUS.get(name)
    if status is None:
        log.warning("Unknown HEAppE job state %r, treating it as waiting", state)
        return JobStatus.WAITING
    return status


def job_data_from_heappe(info: dict[str, Any], job_type: JobType) -> JobData:
    """Build IQM :class:`JobData` from a HEAppE ``SubmittedJobInfoExt`` dict.

    Args:
        info: HEAppE job info (``Id``, ``State``, times, ``Tasks``).
        job_type: ``"run"`` for pulse jobs, ``"circuit"`` for circuit jobs.

    Returns:
        Job data with status, timeline (source ``"heappe"``) and task error messages.
    """
    timeline = []
    for field, status in (
        ("CreationTime", "created"),
        ("SubmitTime", "submitted"),
        ("StartTime", "started"),
        ("EndTime", "ended"),
    ):
        value = info.get(field)
        if not value:
            continue
        try:
            timestamp = to_utc(value)
        except ValueError:
            continue
        timeline.append(
            TimelineEntry(source=HEAPPE_SOURCE, status=status, timestamp=timestamp)
        )

    messages = [
        JobMessage(
            source=HEAPPE_SOURCE,
            message=f"Task {task.get('Id')}: {task.get(key)}",
        )
        for task in info.get("Tasks") or []
        for key in ("ErrorMessage", "Reason")
        if task.get(key)
    ]
    return JobData(
        id=job_uuid(info["Id"]),
        type=job_type,
        status=iqm_job_status(info.get("State")),
        messages=messages,
        timeline=timeline,
    )


#: Fields a lite observation lacks; same placeholders as ``ObservationData.from_observation_lite``.
_LITE_PLACEHOLDERS: dict[str, Any] = {
    "source": {"type": "source_not_loaded"},
    "tags": ["tags_not_loaded"],
}


def _parse_observations(
    payloads: list[dict[str, Any]], dut_label: str | None
) -> list[ObservationData]:
    """Parse observations, upgrading lite ones (no ``dut_label``) to ObservationData.

    Same result as ``iqm.iqm_server_client.iqm_server_storage._parse_observations``
    (placeholders of ``ObservationData.from_observation_lite``), but without building
    an ``ObservationLite``, whose direct instantiation is deprecated.
    """
    return [
        ObservationData.model_validate(p)
        if "dut_label" in p
        else ObservationData.model_validate(
            {
                **_LITE_PLACEHOLDERS,
                **p,
                "dut_label": dut_label if dut_label is not None else "unknown",
            }
        )
        for p in payloads
    ]


def _unsupported(owner: str, name: str) -> Callable[..., Any]:
    """Method that raises NotImplementedError: not available through HEAppE."""

    def method(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{owner}.{name} is not available through HEAppE/QScheduler."
        )

    method.__name__ = name
    method.__doc__ = (
        "Not available through HEAppE/QScheduler; raises NotImplementedError."
    )
    return method


def _stub_remaining_abstract_methods(cls: type) -> type:
    """Class decorator: implement every abstract method left as 'not supported'.

    Keeps the classes instantiable when IQM adds methods to the interfaces.
    """
    for name in sorted(getattr(cls, "__abstractmethods__", ())):
        setattr(cls, name, _unsupported(cls.__name__, name))
    return abc.update_abstractmethods(cls)


@_stub_remaining_abstract_methods
class HEAppEExecutor(ExecutorInterface):
    """IQM executor that runs jobs and reads station artifacts via HEAppE/QScheduler.

    Args:
        qclient: Client bound to the target backend (see :meth:`QClient.bind`).
        walltime_limit_secs: HEAppE walltime limit of each submitted task.
        job_name: HEAppE job name of submitted jobs.
        use_sessions: Default ``UseSessions`` of submitted tasks; ``submit_job(...,
            use_timeslot=True)`` also turns it on.
    """

    def __init__(
        self,
        qclient: QClient,
        *,
        walltime_limit_secs: int = 3600,
        job_name: str = "qpulla_job",
        use_sessions: bool = False,
    ):
        self.qclient = qclient
        self.walltime_limit_secs = walltime_limit_secs
        self.job_name = job_name
        self.use_sessions = use_sessions
        # IQM job id -> (job type, HEAppE task id, payload) for jobs submitted here.
        self._jobs: dict[UUID, tuple[JobType, int | None, JobDefinition | None]] = {}
        # Station artifacts do not change while connected; fetched once per executor.
        self._cache: dict[str, Any] = {}

    def _cached(self, key: str, load: Callable[[], Any]) -> Any:
        """Value of ``load()``, computed on first use and kept for this executor."""
        if key not in self._cache:
            self._cache[key] = load()
        return self._cache[key]

    # ---------- station artifacts ----------

    def _artifact(self, name: MachineArtifactIQM) -> bytes:
        """Raw body of a machine artifact."""
        return self.qclient._run(self.qclient.machine_artifact(name)).body

    def _artifact_json(self, name: MachineArtifactIQM) -> Any:
        """Machine artifact decoded as JSON."""
        body = self._artifact(name)
        try:
            return json.loads(body)
        except ValueError:
            raise QException(
                f"Machine artifact '{name.value}' is not valid JSON"
            ) from None

    def get_experiment_configuration(self) -> dict[str, Any]:
        """Not served by QScheduler; returns an empty configuration."""
        return {}

    def get_or_create_software_version_set(
        self, software_version_set: SoftwareVersionSet
    ) -> int:
        """Software version sets are not stored remotely; always ``0`` (as IQM Server)."""
        return 0

    def _first_of_list(self, name: MachineArtifactIQM) -> Any:
        """First item of a JSON-list artifact (the backend returns a list of one item)."""
        items = self._artifact_json(name)
        if not isinstance(items, list) or not items:
            raise NotFoundError(f"Backend returned no {name.value}")
        return items[0]

    def get_dut_label(self) -> str:
        """Chip label from the chip design record."""
        return self.get_chip_design_record()["cheddar"]["chip_label"]

    def get_chip_design_record(self) -> dict[str, Any]:
        """Chip design record."""
        return self._cached(
            "chip_design_record",
            lambda: self._first_of_list(MachineArtifactIQM.CHIP_DESIGN_RECORDS),
        )

    def get_channel_properties(self) -> dict[str, ChannelProperties]:
        """Control channel properties (protobuf)."""
        return self._cached(
            "channel_properties",
            lambda: unpack_channel_properties(
                self._artifact(MachineArtifactIQM.CHANNEL_PROPERTIES)
            ),
        )

    def get_static_quantum_architecture(self) -> StaticQuantumArchitecture:
        """Static quantum architecture."""
        return self._cached(
            "static_quantum_architecture",
            lambda: StaticQuantumArchitecture.model_validate(
                self._first_of_list(MachineArtifactIQM.STATIC_QUANTUM_ARCHITECTURES)
            ),
        )

    def get_settings(self) -> SettingNode:
        """Station settings; a fresh copy on every call (the compiler modifies it)."""
        settings: SettingNode = self._cached(
            "settings",
            lambda: deserialize_setting_node(
                self._artifact(MachineArtifactIQM.SETTINGS)
            ),
        )
        return settings.model_copy(deep=True)

    # ---------- jobs ----------

    def submit_job(
        self, job_definition: JobDefinition, *, use_timeslot: bool = False
    ) -> JobTracker:
        """Submit a pulse-level ``RunDefinition`` or a ``CircuitJobDefinition`` as a HEAppE job.

        Args:
            job_definition: Job to run.
            use_timeslot: Run the task in a QScheduler session (``UseSessions``).

        Returns:
            :class:`RunJobTracker` or :class:`CircuitJobTracker` for the job.

        Raises:
            TypeError: Unsupported job definition type.
            QException: Submission failed.
        """
        task: dict[str, Any] = {}
        if isinstance(job_definition, RunDefinition):
            # Same as IQMServerExecutor: fresh run and sweep ids for every submission.
            job_definition.run_id = uuid.uuid4()
            job_definition.sweep_definition.sweep_id = uuid.uuid4()
            job_type: JobType = "run"
            kind = QSchedulerTaskKind.PULSE
            payload = serialize_run_definition(job_definition).SerializeToString()
            tracker_cls: type[JobTracker] = RunJobTracker
        elif isinstance(job_definition, CircuitJobDefinition):
            job_type = "circuit"
            kind = QSchedulerTaskKind.CIRCUIT
            payload = job_definition.model_dump_json().encode("utf-8")
            task["EnvironmentVariables"] = {"RUN_SHOTS": str(job_definition.shots)}
            tracker_cls = CircuitJobTracker
        else:
            raise TypeError(
                f"Unsupported job definition type: {type(job_definition).__name__}"
            )

        part_name = f"{kind.value}_payload"
        task.update(
            {
                "Name": f"qpulla_{kind.value}_task",
                "MachineId": self.qclient.get_quantum_backend_info().backend_name,
                "WalltimeLimitSecs": self.walltime_limit_secs,
                "PayloadPartName": part_name,
                "UseSessions": bool(use_timeslot or self.use_sessions),
                "Kind": kind.value,
            }
        )
        log.debug(
            "Submitting %s job (%d bytes) as HEAppE job '%s'",
            kind.value,
            len(payload),
            self.job_name,
        )
        submitted = self.qclient._run(
            self.qclient.submit_quantum_job(
                tasks=[task],
                job_name=self.job_name,
                circuit_payloads={part_name: payload},
            )
        )
        if not isinstance(submitted, dict) or "Id" not in submitted:
            raise QException(f"Unexpected HEAppE submission response: {submitted!r}")

        job_data = job_data_from_heappe(submitted, job_type)
        tasks = submitted.get("Tasks") or []
        task_id = tasks[0].get("Id") if tasks else None
        self._jobs[job_data.id] = (job_type, task_id, job_definition)
        return tracker_cls(job_data=job_data, _payload=job_definition, _executor=self)

    def _job_type(self, job_id: UUID) -> JobType:
        known = self._jobs.get(job_id)
        return known[0] if known else "run"

    def _task_id(self, job_id: StrUUID) -> int:
        """HEAppE task id of a job (looked up once if the job was not submitted here)."""
        job_id = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        job_type, task_id, payload = self._jobs.get(job_id, ("run", None, None))
        if task_id is None:
            info = self.qclient.get_job_info(heappe_job_id(job_id))
            tasks = info.get("Tasks") or []
            if not tasks:
                raise QException(f"HEAppE job {heappe_job_id(job_id)} has no tasks")
            task_id = tasks[0]["Id"]
            self._jobs[job_id] = (job_type, task_id, payload)
        return task_id

    def get_job(self, job_id: StrUUID) -> JobData:
        """Current job data from HEAppE."""
        job_id = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        info = self.qclient.get_job_info(heappe_job_id(job_id))
        return job_data_from_heappe(info, self._job_type(job_id))

    def get_job_payload(self, job_id: StrUUID, job_type: JobType) -> JobDefinition:
        """Payload of a job submitted by this executor.

        Raises:
            NotImplementedError: The job was not submitted here (HEAppE does not return payloads).
        """
        job_id = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        known = self._jobs.get(job_id)
        if known is None or known[2] is None:
            raise NotImplementedError(
                "HEAppE/QScheduler does not return job payloads; only jobs submitted "
                "by this client are known."
            )
        return known[2]

    def _task_artifact(self, job_id: StrUUID, name: str) -> bytes:
        return self.qclient._run(
            self.qclient.get_task_artifact(self._task_id(job_id), name, raw=True)
        )

    def get_artifact_sweep_results(self, job_id: StrUUID) -> SweepResults:
        """Protobuf sweep results of a pulse job."""
        return deserialize_sweep_results(
            self._task_artifact(job_id, SWEEP_RESULTS_ARTIFACT)
        )

    def get_artifact_measurements(
        self, job_id: StrUUID
    ) -> list[CircuitMeasurementResults]:
        """Per-shot measurement results of a circuit job."""
        return CircuitMeasurementResultsBatchAdapter.validate_json(
            self._task_artifact(job_id, MEASUREMENTS_ARTIFACT)
        )

    def get_artifact_measurement_counts(
        self, job_id: StrUUID
    ) -> list[CircuitMeasurementCounts]:
        """Measurement counts of a circuit job."""
        return CircuitCountsBatchAdapter.validate_json(
            self._task_artifact(job_id, MEASUREMENT_COUNTS_ARTIFACT)
        )

    def cancel_job(self, job_id: StrUUID) -> None:
        """Cancel the HEAppE job."""
        self.qclient._run(self.qclient.cancel_job(heappe_job_id(job_id)))


@_stub_remaining_abstract_methods
class HEAppEStorage(StorageInterface):
    """IQM storage that reads calibration data via HEAppE/QScheduler (read-only).

    Args:
        qclient: Client bound to the target backend.
        executor: Executor of the same backend; its chip design record is reused.
    """

    def __init__(self, qclient: QClient, executor: HEAppEExecutor):
        self.qclient = qclient
        self.executor = executor

    def get_chip_design_record(self, dut_label: str) -> dict[str, Any]:
        """Chip design record of the backend's chip.

        Raises:
            NotFoundError: ``dut_label`` is not the backend's chip.
        """
        record = self.executor.get_chip_design_record()
        if record["cheddar"]["chip_label"] != dut_label:
            raise NotFoundError(f"Chip design record for DUT '{dut_label}' not found.")
        return copy.deepcopy(record)

    def get_calibration_set(
        self, calibration_set_id: StrUUIDOrDefault
    ) -> CalibrationSet:
        """Whole calibration set with all its observations."""
        payload = self.qclient.get_machine_calibration_set(
            calibration_set_id=calibration_set_id
        )
        observations = _parse_observations(
            payload.get("observations", []), dut_label=payload.get("dut_label")
        )
        return CalibrationSet.model_validate({**payload, "observations": observations})

    def get_dynamic_quantum_architecture(
        self, calibration_set_id: StrUUIDOrDefault
    ) -> DynamicQuantumArchitecture:
        """Dynamic quantum architecture of a calibration set."""
        raw = self.qclient._run(
            self.qclient.machine_calibration(
                calibration_set_id=calibration_set_id,
                endpoint=MachineCalibrationIQMEndpoint.DYNAMIC_QUANTUM_ARCHITECTURE,
            )
        )
        return DynamicQuantumArchitecture.model_validate_json(raw)

    def get_calibration_set_quality_metric_set(
        self, calibration_set_id: StrUUIDOrDefault
    ) -> QualityMetricSet:
        """Quality metrics of a calibration set."""
        payload = self.qclient.get_machine_metrics(
            calibration_set_id=calibration_set_id
        )
        observations = _parse_observations(
            payload.get("observations", []), dut_label=payload.get("dut_label")
        )
        return QualityMetricSet.model_validate(
            {**payload, "observations": observations}
        )
