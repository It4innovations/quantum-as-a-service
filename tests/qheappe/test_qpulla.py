"""Offline tests for pulse-level access (QPulla) through HEAppE/QScheduler v0.2.2.

* ``QClient`` requests for machine artifacts, calibration sets and raw task artifacts
  are checked against a fake ``_request``.
* ``HEAppEExecutor`` / ``HEAppEStorage`` / ``QPulla`` run against a fake ``QClient``.

Needs the IQM 36 stack (skipped otherwise).
"""

import asyncio
import json
import uuid

import numpy as np
import pytest
import yarl

pytest.importorskip("iqm.cpc", reason="QPulla needs the IQM 36 stack")

from iqm.iqm_client import IQMClient
from iqm.station_control.client.job_trackers import (
    CircuitJobTracker,
    RunJobTracker,
)
from iqm.station_control.client.serializers import serialize_sweep_results
from iqm.station_control.interface.models import (
    CircuitJobDefinition,
    RunDefinition,
)
from iqm.station_control.interface.models.jobs_new import JobStatus

from qheappe_client.backend_metadata import LexisResource, QCluster
from qheappe_client.qclient import (
    HttpResult,
    MachineArtifactIQM,
    QClient,
    QSchedulerTaskKind,
)
from qheappe_client.qpulla import QPulla, heappe_iqm
from qheappe_client.qpulla.heappe_iqm import (
    HEAppEExecutor,
    HEAppEStorage,
    heappe_job_id,
    iqm_job_status,
    job_data_from_heappe,
    job_uuid,
)
from qheappe_client.utils import QException

# HEAppE job states (HEAppEJobState values)
QUEUED, RUNNING, FINISHED, FAILED, CANCELED = 4, 8, 16, 32, 64

CHIP_DESIGN_RECORD = {"cheddar": {"chip_label": "Q5_chip"}}


# ---------- QClient: new QScheduler v0.2.2 requests ----------


def recording_client(routes: dict) -> tuple[QClient, list[dict]]:
    """Connected client with one backend; ``_request`` is faked and records every call."""
    vlq = QCluster(
        name="VLQ",
        heappe_url="https://h1",
        cluster_id=1,
        cluster_node_type_id=2,
        heappe_project_id=3,
    )
    c = QClient("tok", "proj")
    c._authenticated = True
    c._lexis_resource = LexisResource("Q", "pr", "-", "-", {"VLQ": vlq})
    c._active_cluster = "VLQ"
    calls: list[dict] = []

    async def fake_request(method, url, **kw):
        calls.append({"method": method, "path": yarl.URL(url).path, **kw})
        for suffix, resp in routes.items():
            if yarl.URL(url).path.endswith(suffix):
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        raise AssertionError(f"unexpected request {method} {url}")

    c._request = fake_request
    return c, calls


def body(payload: bytes, content_type="application/octet-stream") -> HttpResult:
    return HttpResult(200, content_type, payload)


class TestQClientPulseRequests:
    def test_protobuf_artifact_sends_accept_header(self):
        c, calls = recording_client({"/QScheduler/MachineArtifact": body(b"\x0a\x01")})
        res = c.get_machine_artifact(MachineArtifactIQM.SETTINGS)
        assert res.body == b"\x0a\x01"
        (call,) = calls
        assert call["method"] == "GET"
        assert call["params"] == {
            "ClusterNodeTypeId": 2,
            "ArtifactName": "settings",
            "ProjectId": 3,
        }
        assert call["headers"] == {"Accept": "application/protobuf"}

    def test_json_artifact_has_no_accept_header(self):
        c, calls = recording_client({"/QScheduler/MachineArtifact": body(b"[]")})
        c.get_machine_artifact("duts")
        assert calls[0]["params"]["ArtifactName"] == "duts"
        assert calls[0]["headers"] is None

    def test_explicit_accept_wins(self):
        c, calls = recording_client({"/QScheduler/MachineArtifact": body(b"{}")})
        c.get_machine_artifact(
            MachineArtifactIQM.CHANNEL_PROPERTIES, accept="application/json"
        )
        assert calls[0]["headers"] == {"Accept": "application/json"}

    def test_calibration_set_is_sent_without_endpoint(self):
        payload = {"observations": [], "calibration_set_id": str(uuid.uuid4())}
        c, calls = recording_client(
            {"/QScheduler/MachineCalibration": body(json.dumps(payload).encode())}
        )
        assert c.get_machine_calibration_set() == payload
        params = calls[0]["params"]
        assert params["CalibrationId"] == "default"
        assert params.get("Endpoint") is None  # dropped from the query string

    def test_calibration_set_must_be_object(self):
        c, _ = recording_client({"/QScheduler/MachineCalibration": body(b"[1]")})
        with pytest.raises(QException, match="not a JSON object"):
            c.get_machine_calibration_set()

    def test_heappe_requiring_endpoint_gives_clear_error(self):
        rejected = QException("HTTP 400: Endpoint must be provided.")
        c, _ = recording_client({"/QScheduler/MachineCalibration": rejected})
        with pytest.raises(QException, match="cannot return a whole calibration set"):
            c.get_machine_calibration_set()

    def test_raw_task_artifact_skips_json_decoding(self):
        c, _ = recording_client({"/QScheduler/GetTaskArtifact": body(b"[1, 2]")})
        assert (
            asyncio.run(c.get_task_artifact(7, "sweep_results", raw=True)) == b"[1, 2]"
        )
        assert asyncio.run(c.get_task_artifact(7, "measurements")) == [1, 2]

    def test_extra_headers_keep_authorization(self):
        seen: dict = {}

        class Resp:
            status = 200
            content_type = "application/octet-stream"
            url = yarl.URL("https://h1/x")

            async def read(self):
                return b"ok"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Session:
            closed = False

            def request(self, method, url, **kw):
                seen.update(kw["headers"])
                return Resp()

        c = QClient("tok", "proj")
        c._session = Session()
        res = asyncio.run(
            c._request_impl(
                "GET", "https://h1/x", headers={"Accept": "a", "Authorization": "x"}
            )
        )
        assert res.body == b"ok"
        assert seen == {"Authorization": "Bearer tok", "Accept": "a"}

    def test_task_kind_values(self):
        assert QSchedulerTaskKind.PULSE.value == "pulse"
        assert QSchedulerTaskKind.CIRCUIT.value == "circuit"


# ---------- HEAppE job info -> IQM job data ----------


class TestJobMapping:
    def test_uuid_round_trip(self):
        assert heappe_job_id(job_uuid(1234)) == 1234
        assert heappe_job_id(str(job_uuid(5))) == 5

    @pytest.mark.parametrize(
        ("state", "status"),
        [
            (1, JobStatus.WAITING),
            (2, JobStatus.WAITING),
            (QUEUED, JobStatus.WAITING),
            (128, JobStatus.WAITING),
            (RUNNING, JobStatus.PROCESSING),
            (FINISHED, JobStatus.COMPLETED),
            (FAILED, JobStatus.FAILED),
            (CANCELED, JobStatus.CANCELLED),
            ("Finished", JobStatus.COMPLETED),
            ("16", JobStatus.COMPLETED),
            (999, JobStatus.WAITING),
            (None, JobStatus.WAITING),
        ],
    )
    def test_status(self, state, status):
        assert iqm_job_status(state) is status

    def test_job_data(self):
        info = {
            "Id": 77,
            "State": FAILED,
            "CreationTime": "2026-11-05T10:00:00",
            "StartTime": "2026-11-05T10:00:05Z",
            "EndTime": "not a date",
            "Tasks": [{"Id": 9, "ErrorMessage": "pulse access disabled"}],
        }
        data = job_data_from_heappe(info, "run")
        assert data.id == job_uuid(77)
        assert data.type == "run"
        assert data.status is JobStatus.FAILED
        assert [(e.source, e.status) for e in data.timeline] == [
            ("heappe", "created"),
            ("heappe", "started"),
        ]
        assert data.messages[0].message == "Task 9: pulse access disabled"


# ---------- executor / storage / QPulla against a fake QClient ----------


class FakeQClient:
    """Stands in for a QClient bound to backend 'VLQ'."""

    def __init__(self, artifacts=None, states=(FINISHED,), task_artifacts=None):
        self.artifacts = artifacts or {}
        self.task_artifacts = task_artifacts or {}
        self.states = list(states)
        self.submitted: list[dict] = []
        self.cancelled: list[int] = []
        self.artifact_requests: list[str] = []
        self.calibration_requests: list[tuple] = []

    def _run(self, coro):
        return asyncio.run(coro)

    def get_quantum_backend_info(self):
        return type("Md", (), {"backend_name": "VLQ", "machine_info": None})()

    async def machine_artifact(self, name, accept=None, **_):
        name = getattr(name, "value", name)
        self.artifact_requests.append(name)
        return HttpResult(200, "application/octet-stream", self.artifacts[name])

    async def submit_quantum_job(self, tasks, job_name, circuit_payloads):
        self.submitted.append(
            {"tasks": tasks, "job_name": job_name, "payloads": circuit_payloads}
        )
        return {"Id": 500 + len(self.submitted), "State": 2, "Tasks": [{"Id": 42}]}

    def get_job_info(self, job_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"Id": job_id, "State": state, "Tasks": [{"Id": 42}]}

    async def get_task_artifact(self, task_id, name, backend_name=None, *, raw=False):
        assert raw and task_id == 42
        return self.task_artifacts[name]

    async def cancel_job(self, job_id, backend_name=None):
        self.cancelled.append(job_id)
        return True

    def get_machine_calibration_set(self, backend_name=None, calibration_set_id=None):
        self.calibration_requests.append(("set", calibration_set_id))
        return {
            "observation_set_id": str(uuid.UUID(int=1)),
            "observation_set_type": "calibration-set",
            "dut_label": "Q5_chip",
            "created_timestamp": "2026-11-05T10:00:00Z",
            "end_timestamp": "2026-11-05T10:05:00Z",
            "observations": [],
        }

    def get_machine_metrics(self, backend_name=None, calibration_set_id=None):
        self.calibration_requests.append(("metrics", calibration_set_id))
        return {"observations": []}


def run_definition() -> RunDefinition:
    """Bare RunDefinition (dataclass, built without __init__); serialization is stubbed."""
    rd = object.__new__(RunDefinition)
    rd.run_id = None
    rd.sweep_definition = type("Sd", (), {"sweep_id": None})()
    return rd


@pytest.fixture
def stub_run_serializer(monkeypatch):
    class Proto:
        def __init__(self, rd):
            self.rd = rd

        def SerializeToString(self):
            return b"\x0aRUNDEF"

    monkeypatch.setattr(heappe_iqm, "serialize_run_definition", Proto)


class TestExecutor:
    def test_chip_design_record_and_label(self):
        q = FakeQClient(
            {"chip-design-records": json.dumps([CHIP_DESIGN_RECORD]).encode()}
        )
        ex = HEAppEExecutor(q)
        assert ex.get_dut_label() == "Q5_chip"
        assert ex.get_chip_design_record() == CHIP_DESIGN_RECORD
        assert q.artifact_requests == ["chip-design-records"]  # cached

    def test_invalid_json_artifact(self):
        ex = HEAppEExecutor(FakeQClient({"chip-design-records": b"\xff"}))
        with pytest.raises(QException, match="not valid JSON"):
            ex.get_chip_design_record()

    def test_pulse_submission(self, stub_run_serializer):
        q = FakeQClient()
        ex = HEAppEExecutor(q, walltime_limit_secs=600, job_name="pulse_job")
        rd = run_definition()

        tracker = ex.submit_job(rd)

        assert isinstance(tracker, RunJobTracker)
        assert tracker.job_id == job_uuid(501)
        assert tracker.status is JobStatus.WAITING
        assert isinstance(rd.run_id, uuid.UUID)
        assert isinstance(rd.sweep_definition.sweep_id, uuid.UUID)
        (sub,) = q.submitted
        (task,) = sub["tasks"]
        assert sub["job_name"] == "pulse_job"
        assert task["Kind"] == "pulse"
        assert task["MachineId"] == "VLQ"
        assert task["WalltimeLimitSecs"] == 600
        assert task["UseSessions"] is False
        assert "EnvironmentVariables" not in task
        assert sub["payloads"] == {task["PayloadPartName"]: b"\x0aRUNDEF"}
        assert tracker.payload() is rd

    def test_use_timeslot_turns_on_sessions(self, stub_run_serializer):
        q = FakeQClient()
        HEAppEExecutor(q).submit_job(run_definition(), use_timeslot=True)
        assert q.submitted[0]["tasks"][0]["UseSessions"] is True

    def test_circuit_submission(self):
        q = FakeQClient()
        job = CircuitJobDefinition.model_construct(shots=100)
        tracker = HEAppEExecutor(q).submit_job(job)
        assert isinstance(tracker, CircuitJobTracker)
        task = q.submitted[0]["tasks"][0]
        assert task["Kind"] == "circuit"
        assert task["EnvironmentVariables"] == {"RUN_SHOTS": "100"}
        payload = q.submitted[0]["payloads"][task["PayloadPartName"]]
        assert json.loads(payload)["shots"] == 100

    def test_unsupported_job(self):
        with pytest.raises(TypeError, match="Unsupported"):
            HEAppEExecutor(FakeQClient()).submit_job(object())

    def test_wait_and_fetch_sweep_results(self, stub_run_serializer):
        sweep = {"QB1__readout.result": [np.array([1.0, 0.0, 1.0])]}
        q = FakeQClient(
            states=(QUEUED, RUNNING, FINISHED),
            task_artifacts={
                "sweep_results": serialize_sweep_results(uuid.uuid4(), sweep)
            },
        )
        tracker = HEAppEExecutor(q).submit_job(run_definition())

        status = tracker.wait_for_completion(polling_interval=0, timeout_secs=5)

        assert status is JobStatus.COMPLETED
        result = tracker.result_sweep()
        np.testing.assert_array_equal(
            result["QB1__readout.result"][0], sweep["QB1__readout.result"][0]
        )

    def test_failed_job_has_no_results(self, stub_run_serializer):
        q = FakeQClient(states=(FAILED,))
        tracker = HEAppEExecutor(q).submit_job(run_definition())
        assert tracker.wait_for_completion(polling_interval=0) is JobStatus.FAILED
        assert tracker.result_sweep() is None

    def test_cancel(self, stub_run_serializer):
        q = FakeQClient()
        HEAppEExecutor(q).submit_job(run_definition()).cancel()
        assert q.cancelled == [501]

    def test_unknown_job_payload(self):
        with pytest.raises(NotImplementedError, match="payloads"):
            HEAppEExecutor(FakeQClient()).get_job_payload(job_uuid(3), "run")

    def test_task_id_looked_up_for_foreign_job(self):
        q = FakeQClient(task_artifacts={"measurement_counts": b"[]"})
        assert HEAppEExecutor(q).get_artifact_measurement_counts(job_uuid(3)) == []

    def test_unsupported_methods_raise(self):
        with pytest.raises(NotImplementedError, match="HEAppE"):
            HEAppEExecutor(FakeQClient()).delete_job(job_uuid(1))


class TestStorage:
    def test_calibration_set(self):
        q = FakeQClient()
        cs = HEAppEStorage(q, HEAppEExecutor(q)).get_calibration_set("default")
        assert cs.observations == []
        assert q.calibration_requests == [("set", "default")]

    def test_chip_design_record_label_check(self):
        q = FakeQClient(
            {"chip-design-records": json.dumps([CHIP_DESIGN_RECORD]).encode()}
        )
        storage = HEAppEStorage(q, HEAppEExecutor(q))
        assert storage.get_chip_design_record("Q5_chip") == CHIP_DESIGN_RECORD
        with pytest.raises(Exception, match="not found"):
            storage.get_chip_design_record("other")

    def test_write_methods_unsupported(self):
        q = FakeQClient()
        with pytest.raises(NotImplementedError):
            HEAppEStorage(q, HEAppEExecutor(q)).query_observations()


class TestQPulla:
    def test_is_iqm_client(self):
        q = FakeQClient()
        pulla = QPulla(q, walltime_limit_secs=120, use_sessions=True)
        assert isinstance(pulla, IQMClient)
        assert pulla.quantum_computer_name == "VLQ"
        assert pulla.executor.walltime_limit_secs == 120
        assert pulla.executor.use_sessions is True
        assert repr(pulla) == "QPulla(backend='VLQ')"

    def test_calibration_set_stash_uses_heappe(self):
        q = FakeQClient()
        stash = QPulla(q).get_calibration_set_stash()
        assert q.calibration_requests == [("set", "default")]
        assert stash.observations == {}

    def test_submit_playlist_goes_through_heappe(self, stub_run_serializer):
        q = FakeQClient()
        job = QPulla(q).submit_playlist(run_definition(), context={"k": 1})
        assert isinstance(job, RunJobTracker)
        assert job._context == {"k": 1}
        assert q.submitted[0]["tasks"][0]["Kind"] == "pulse"

    def test_get_health_unavailable(self):
        with pytest.raises(NotImplementedError):
            QPulla(FakeQClient()).get_health()


def test_top_level_export():
    import qheappe_client

    assert qheappe_client.QPulla is QPulla
