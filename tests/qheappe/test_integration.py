"""Live end-to-end tests for qheappe_client against LEXIS / HEAppE QScheduler.

Public surface exercised:
  QProvider(lexis_project, token, lexis_api_url).get_backend / get_client / ...
  QBackendIQM.architecture / .transpile() / .create_run_request() / .run()
  QJob.result() (qiskit Result), QJob.status(), QJob.cancel()
  QSamplerV2, and qiskit BackendSamplerV2 on a QaaS backend
  QClient sync and async API

Tests marked `qpu` submit jobs and consume allocation. TestFixedIssues keeps
regression tests for defects that were fixed.
QPulla is covered in test_integration_qpulla.py.

Run: pytest -m integration -v
"""

import asyncio
import time
import warnings
from collections import Counter

import numpy as np
import pytest
from conftest import close_provider, make_provider
from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
from qiskit.circuit import Parameter
from qiskit.compiler import transpile as qiskit_transpile
from qiskit.primitives import BackendSamplerV2, PrimitiveResult
from qiskit.providers.jobstatus import JobStatus
from qiskit.result import Result
from qiskit.transpiler.exceptions import TranspilerError
from settings import (
    BACKEND_NAME,
    CLIENT_ARGS,
    INVALID_RESOURCE,
    JOB_TIMEOUT,
    LEXIS_API_URL,
    LIVE,
    MIN_FIDELITY,
    PROJECT,
    RESOURCE,
    SKIP_REASON,
    TOKEN,
)

from qheappe_client import QBackendIQM, QBackendMetadata, QSamplerV2
from qheappe_client.qclient import QClient
from qheappe_client.qiskit.qbackend import QJob
from qheappe_client.utils import HEAppEJobState, QAuthException, QException

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not LIVE, reason=SKIP_REASON)]

TERMINAL = QClient.TERMINAL_JOB_STATES
# Qiskit op names produced by the IQM transpiler (prx -> "r").
NATIVE_OPS = {"r", "cz", "move", "measure", "barrier", "delay", "reset", "id"}


def legacy_counts(job: QJob) -> dict[str, int]:
    return job.result().get_counts()


def fraction(counts: dict[str, int], keys) -> float:
    return sum(counts.get(k, 0) for k in keys) / sum(counts.values())


def reattach(backend, job: QJob) -> QJob:
    """Fresh QJob for an existing HEAppE job id.

    Each result format is read through a fresh QJob, because a second format on the
    same QJob object re-waits on the event stream (see TestKnownIssues).
    """
    return QJob(backend, int(job.job_id))


def heappe_state(client, job_id) -> str:
    return HEAppEJobState.readable(client.get_job_info(int(job_id))["State"])


# ---------- 1. Provider / auth ----------


class TestProvider:
    def test_init_with_explicit_token_does_no_io(self):
        make_provider(token="not-a-valid-token")

    def test_invalid_token_raises_auth(self):
        p = make_provider(token="not-a-valid-token")
        with pytest.raises(QAuthException):
            p.get_backend(RESOURCE, BACKEND_NAME, **CLIENT_ARGS)

    def test_unknown_project_raises_auth(self):
        p = make_provider(project="definitely-not-a-real-project")
        with pytest.raises(QAuthException, match="does not have access"):
            p.get_backend(RESOURCE, BACKEND_NAME, **CLIENT_ARGS)

    def test_client_cached_per_resource(self, provider):
        c1 = provider._get_client(RESOURCE)
        assert provider._get_client(RESOURCE) is c1
        assert c1.is_authenticated

    def test_clear_cache_creates_new_client(self):
        p = make_provider()
        try:
            c1 = p._get_client(RESOURCE)
            p.clear_cache()
            c2 = p._get_client(RESOURCE)
            assert c2 is not c1
            c1.close()
        finally:
            close_provider(p)


# ---------- 2. Backend retrieval ----------


class TestGetBackend:
    def test_valid_resource(self, backend):
        assert isinstance(backend, QBackendIQM)

    def test_invalid_resource_raises(self, provider):
        with pytest.raises(QAuthException, match="not found"):
            provider.get_backend(INVALID_RESOURCE, BACKEND_NAME, **CLIENT_ARGS)

    def test_empty_resource_raises(self, provider):
        with pytest.raises(QException):
            provider.get_backend("", BACKEND_NAME, **CLIENT_ARGS)

    def test_unknown_backend_name_raises(self, provider):
        with pytest.raises(QException, match="Unknown backend"):
            provider.get_backend(RESOURCE, "NO-SUCH-BACKEND")

    def test_from_metadata_object(self, provider, backend):
        md = backend._backend_metadata
        b = provider.get_backend(md)
        assert b._backend_metadata.backend_name == md.backend_name

    def test_metadata_name_conflict_raises(self, provider, backend):
        with pytest.raises(ValueError, match="conflicts"):
            provider.get_backend(backend._backend_metadata, "NO-SUCH-BACKEND")

    def test_pinned_calibration_set(self, provider, backend):
        cal_id = backend.architecture.calibration_set_id
        b = provider.get_backend(RESOURCE, BACKEND_NAME, calibration_set_id=cal_id)
        assert b.architecture.calibration_set_id == cal_id

    def test_use_metrics(self, provider):
        b = provider.get_backend(RESOURCE, BACKEND_NAME, use_metrics=True)
        assert isinstance(b, QBackendIQM)


# ---------- 3. Metadata ----------


class TestBackendMetadata:
    @pytest.fixture
    def md(self, backend) -> QBackendMetadata:
        return backend._backend_metadata

    def test_identity(self, md):
        assert md.lexis_resource.resource_name == RESOURCE
        assert md.lexis_project.project_name == PROJECT
        if BACKEND_NAME:
            assert md.backend_name == BACKEND_NAME

    def test_available_iqm(self, md):
        assert md.available, md.error
        assert md.software_stack == "iqm"
        assert (md.supplier, md.quantum_technology) == ("IQM", "SUPERCONDUCTING")
        assert md.qaas_version

    def test_heappe_ids_resolved(self, md):
        assert md.cluster_id is not None
        assert md.cluster_node_type_id is not None

    def test_architectures_loaded(self, md):
        assert md.static_architecture is not None
        assert md.dynamic_architecture is not None


# ---------- 4. Client ----------


class TestClient:
    def test_bound_client(self, client, backend):
        assert client.is_authenticated
        assert client.active_backend == backend._backend_metadata.backend_name
        assert client.active_backend in client.backend_names

    def test_available_backends(self, client):
        mds = client.get_available_backends(with_architecture=False)
        assert set(mds) == set(client.backend_names)
        assert all(isinstance(m, QBackendMetadata) for m in mds.values())
        assert mds[client.active_backend].available

    def test_machine_metrics(self, client):
        metrics = client.get_machine_metrics()
        assert isinstance(metrics, (dict, list)) and len(metrics) > 0

    def test_unknown_job_info_raises(self, client):
        with pytest.raises(QException):
            client.get_job_info(2**31 - 1)

    def test_async_api_roundtrip(self):
        async def main():
            async with await QClient.create(
                TOKEN, PROJECT, RESOURCE, lexis_api_url=LEXIS_API_URL, **CLIENT_ARGS
            ) as c:
                md = await c.get_backend(BACKEND_NAME)
                assert md.available and c.active_backend == md.backend_name
                assert md.dynamic_architecture is not None
                metrics = await c.machine_metrics()
                assert metrics
            assert not c.is_authenticated

        asyncio.run(main())


# ---------- 5. Architecture ----------


class TestArchitecture:
    def test_has_qubits(self, backend):
        assert len(backend.architecture.qubits) > 0

    def test_has_gates(self, backend):
        assert len(backend.architecture.gates) > 0

    def test_stable_across_calls(self, backend):
        assert list(backend.architecture.qubits) == list(backend.architecture.qubits)

    def test_target_matches_architecture(self, backend):
        assert backend.num_qubits == len(backend.architecture.qubits)


# ---------- 6. Transpile ----------


class TestTranspile:
    def test_returns_circuit(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        assert isinstance(tc, QuantumCircuit) and tc.num_qubits > 0

    def test_list_input(self, backend, bell_circuit, ghz_circuit):
        out = backend.transpile([bell_circuit, ghz_circuit])
        assert isinstance(out, list) and len(out) == 2

    def test_uses_native_gate_set(self, backend, bell_circuit):
        used = {i.operation.name for i in backend.transpile(bell_circuit).data}
        assert used <= NATIVE_OPS, f"non-native ops: {used - NATIVE_OPS}"

    def test_optimize_single_qubits(self, backend, bell_circuit):
        default = backend.transpile(bell_circuit)
        optimized = backend.transpile(bell_circuit, optimize_single_qubits=True)
        assert len(optimized.data) <= len(default.data)

    def test_empty_circuit(self, backend):
        assert backend.transpile(QuantumCircuit(1, 1)) is not None

    def test_too_wide_circuit_raises(self, backend):
        qc = QuantumCircuit(backend.num_qubits + 1)
        qc.h(range(qc.num_qubits))
        with pytest.raises((QException, TranspilerError, ValueError)):
            backend.transpile(qc)


# ---------- 7. Run request validation (no submission) ----------


class TestRunRequest:
    def test_valid_request(self, backend, bell_circuit):
        rr = backend.create_run_request(backend.transpile(bell_circuit), shots=10)
        assert rr.shots == 10 and len(rr.circuits) == 1

    def test_untranspiled_circuit_rejected(self, backend, bell_circuit):
        with pytest.raises(Exception, match="(?i)valid|support|transpil"):
            backend.create_run_request(bell_circuit, shots=10)

    def test_zero_shots_rejected(self, backend, bell_circuit):
        with pytest.raises(ValueError, match="shots"):
            backend.create_run_request(backend.transpile(bell_circuit), shots=0)

    def test_empty_batch_rejected(self, backend):
        with pytest.raises(ValueError, match="Empty"):
            backend.create_run_request([], shots=10)

    def test_default_shots_option(self, backend):
        assert backend.options.shots == 1024

    def test_run_without_shots_uses_default(self, backend, bell_circuit):
        job = backend.run(backend.transpile(bell_circuit))
        assert sum(job.result().get_counts().values()) == backend.options.shots


# ---------- 8. Job lifecycle and results ----------

BELL_SHOTS = 1000


@pytest.fixture(scope="module")
def bell_job(backend):
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    job = backend.run(
        backend.transpile(qc), shots=BELL_SHOTS, job_name="qheappe_test_bell"
    )
    job.result()  # wait once; tests below reattach for other formats
    return job


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestJob:
    def test_job_handle(self, bell_job):
        assert isinstance(bell_job, QJob)
        assert int(bell_job.job_id) > 0
        assert bell_job.job_id() == str(int(bell_job.job_id))

    def test_status_done_after_result(self, bell_job):
        assert bell_job.status() == JobStatus.DONE

    def test_server_state_finished(self, client, bell_job):
        """HEAppE job info reaches FINISHED. It can lag behind the event stream,
        which result() waits on, so allow a short delay."""
        deadline = time.monotonic() + 60
        seen = []
        while (state := heappe_state(client, bell_job.job_id)) != "FINISHED":
            seen.append(state)
            assert time.monotonic() < deadline, f"job info stuck in {seen[-1]}"
            time.sleep(2)
        if seen:
            warnings.warn(
                f"HEAppE job info lagged behind the event stream: {seen}", stacklevel=1
            )

    def test_result_cached(self, bell_job):
        assert bell_job.result() is bell_job.result()

    def test_legacy_counts_shape(self, backend, bell_job):
        counts = legacy_counts(reattach(backend, bell_job))
        assert sum(counts.values()) == BELL_SHOTS
        assert all(set(k) <= {"0", "1"} and len(k) == 2 for k in counts)

    def test_legacy_memory_matches_counts(self, backend, bell_job):
        res = QJob(backend, int(bell_job.job_id), memory=True).result()
        memory = res.get_memory()
        assert len(memory) == BELL_SHOTS
        assert Counter(memory) == res.get_counts()

    def test_bell_correlation(self, backend, bell_job):
        counts = legacy_counts(reattach(backend, bell_job))
        assert fraction(counts, ("00", "11")) > MIN_FIDELITY

    def test_result_after_job_already_finished(self, backend, client, bell_circuit):
        job = backend.run(backend.transpile(bell_circuit), shots=50)
        deadline = time.monotonic() + 90
        while heappe_state(client, job.job_id) not in TERMINAL:
            assert time.monotonic() < deadline, "job did not finish"
            time.sleep(2)
        assert sum(legacy_counts(job).values()) == 50


# ---------- 8b. run(memory=True) -> per-shot results (issue #49) ----------

MEMORY_SHOTS = 200


@pytest.fixture(scope="module")
def memory_job(backend):
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    job = backend.run(
        backend.transpile(qc),
        shots=MEMORY_SHOTS,
        memory=True,
        job_name="qheappe_test_memory",
    )
    job.result()  # wait once; tests below reattach for other formats
    return job


def reattach_memory(backend, job: QJob, memory: bool = True) -> QJob:
    """Fresh QJob for an existing job, as if submitted with run(memory=...)."""
    return QJob(backend, int(job.job_id), memory=memory)


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestRunMemory:
    def test_job_remembers_memory_option(self, memory_job):
        assert memory_job._memory is True

    def test_result_includes_memory(self, backend, memory_job):
        res = reattach_memory(backend, memory_job).result()
        memory = res.get_memory()
        assert len(memory) == MEMORY_SHOTS
        assert Counter(memory) == res.get_counts()
        # what BackendSamplerV2 reads
        assert res.results[0].data.memory is not None

    def test_memory_shows_bell_correlation(self, backend, memory_job):
        memory = reattach_memory(backend, memory_job).result().get_memory()
        assert fraction(Counter(memory), ("00", "11")) > MIN_FIDELITY

    def test_reattach_without_memory_gives_counts_only(self, backend, memory_job):
        res = reattach_memory(backend, memory_job, memory=False).result()
        assert getattr(res.results[0].data, "memory", None) is None
        assert sum(res.get_counts().values()) == MEMORY_SHOTS

    def test_default_run_has_no_memory(self, backend, bell_job):
        """run() without memory keeps the old behavior: counts only."""
        res = reattach(backend, bell_job).result()
        assert getattr(res.results[0].data, "memory", None) is None


# ---------- 8c. status() queries HEAppE without result() ----------

STATUS_SHOTS = 100
STATUS_POLL = 2


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestLiveStatus:
    """status(), done(), in_final_state() and wait_for_final_state() follow the
    server state without calling result() first, as qaas.client QJob did."""

    def test_status_reaches_done_without_result(self, backend, client, bell_circuit):
        job = backend.run(backend.transpile(bell_circuit), shots=STATUS_SHOTS)
        seen = []
        deadline = time.monotonic() + JOB_TIMEOUT - 30
        while not job.in_final_state():
            seen.append(job.status())
            assert time.monotonic() < deadline, f"stuck in {seen[-1]}"
            time.sleep(STATUS_POLL)
        assert job.status() == JobStatus.DONE and job.done()
        assert JobStatus.ERROR not in seen, seen
        assert heappe_state(client, job.job_id) == "FINISHED"
        # result() still works after status polling
        assert sum(legacy_counts(job).values()) == STATUS_SHOTS

    def test_wait_for_final_state_returns(self, backend, bell_circuit):
        job = backend.run(backend.transpile(bell_circuit), shots=STATUS_SHOTS)
        job.wait_for_final_state(timeout=JOB_TIMEOUT - 30, wait=STATUS_POLL)
        assert job.status() == JobStatus.DONE

    def test_wait_for_final_state_follows_events(self, backend, bell_circuit):
        """Event websocket drives the wait; job-info backup only every 5 minutes."""
        job = backend.run(backend.transpile(bell_circuit), shots=STATUS_SHOTS)
        seen = []
        start = time.monotonic()
        job.wait_for_final_state(
            timeout=JOB_TIMEOUT - 30, wait=300, callback=lambda *a: seen.append(a[1])
        )
        assert time.monotonic() - start < 300, "finished only via the backup query"
        assert job.done()
        assert JobStatus.ERROR not in seen and JobStatus.DONE not in seen, seen
        assert sum(job.result().get_counts().values()) == STATUS_SHOTS

    def test_sampler_job_wait_for_final_state(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = QSamplerV2(backend).run([(tc, None, 50), (tc, None, 80)])
        job.wait_for_final_state(timeout=JOB_TIMEOUT - 30, wait=300)
        assert job.done()
        assert [r.data.meas.num_shots for r in job.result()] == [50, 80]

    def test_reattached_finished_job_is_done(self, backend, bell_job):
        """A fresh QJob for a finished job reports DONE without result()."""
        job = reattach(backend, bell_job)
        assert job.status() == JobStatus.DONE
        assert job.done() and job.in_final_state()


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestCircuits:
    def test_ghz(self, backend, ghz_circuit):
        counts = legacy_counts(backend.run(backend.transpile(ghz_circuit), shots=200))
        assert sum(counts.values()) == 200
        assert fraction(counts, ("000", "111")) > MIN_FIDELITY - 0.1

    def test_multiple_circuits_in_one_job(self, backend, bell_circuit, ghz_circuit):
        job = backend.run(backend.transpile([bell_circuit, ghz_circuit]), shots=100)
        res = job.result()
        assert len(res.results) == 2
        assert [len(next(iter(res.get_counts(i)))) for i in range(2)] == [2, 3]

    def test_sequential_jobs_are_independent(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job1 = backend.run(tc, shots=100)
        c1 = legacy_counts(job1)
        job2 = backend.run(tc, shots=100)
        c2 = legacy_counts(job2)
        assert int(job1.job_id) != int(job2.job_id)
        assert sum(c1.values()) == sum(c2.values()) == 100

    def test_parameter_sweep(self, backend):
        theta = Parameter("theta")
        qc = QuantumCircuit(2)
        qc.ry(theta, 0)
        qc.cx(0, 1)
        qc.measure_all()
        for value in (0, np.pi / 4, np.pi / 2, np.pi):
            tc = qiskit_transpile(
                qc.assign_parameters({theta: value}),
                backend=backend,
                optimization_level=2,
            )
            assert sum(legacy_counts(backend.run(tc, shots=50)).values()) == 50

    def test_zero_rotation_prepares_zero_state(self, backend):
        qc = QuantumCircuit(2)
        qc.ry(0, 0)
        qc.cx(0, 1)
        qc.measure_all()
        counts = legacy_counts(backend.run(backend.transpile(qc), shots=200))
        assert counts.get("00", 0) / 200 > MIN_FIDELITY

    def test_three_sequential_jobs_within_budget(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        start = time.monotonic()
        for _ in range(3):
            legacy_counts(backend.run(tc, shots=50))
        assert time.monotonic() - start < 300


# ---------- 8d. 1.1.0: Result by default, QSamplerV2 (issue #49) ----------

SAMPLER_SHOTS = 200


def one_hot_circuit():
    """x on qubit 0 only; two registers so bit order and register split both matter.

    Expected (Qiskit convention, clbit 0 rightmost): a = "01", b = "0",
    Result counts key = "0 01" (registers separated by a space).
    """
    a, b = ClassicalRegister(2, "a"), ClassicalRegister(1, "b")
    qc = QuantumCircuit(QuantumRegister(3), a, b, name="onehot", metadata={"tag": 1})
    qc.x(0)
    qc.measure([0, 1], a)
    qc.measure(2, b)
    return qc


def top(counts: dict[str, int]) -> tuple[str, float]:
    key = max(counts, key=counts.get)
    return key, counts[key] / sum(counts.values())


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestQSamplerV2:
    def test_result_is_legacy_by_default(self, bell_job):
        assert isinstance(bell_job.result(), Result)

    def test_bell(self, backend, bell_circuit):
        res = (
            QSamplerV2(backend)
            .run([backend.transpile(bell_circuit)], shots=SAMPLER_SHOTS)
            .result()
        )
        assert isinstance(res, PrimitiveResult)
        meas = res[0].data.meas
        assert meas.num_shots == SAMPLER_SHOTS and meas.num_bits == 2
        assert fraction(meas.get_counts(), ("00", "11")) > MIN_FIDELITY

    def test_registers_and_bit_order(self, backend):
        res = (
            QSamplerV2(backend)
            .run([backend.transpile(one_hot_circuit())], shots=SAMPLER_SHOTS)
            .result()
        )
        a, pa = top(res[0].data.a.get_counts())
        b, pb = top(res[0].data.b.get_counts())
        assert (a, b) == ("01", "0"), res[0].data
        assert min(pa, pb) > MIN_FIDELITY - 0.1

    def test_legacy_result_bit_order(self, backend):
        """Result follows the Qiskit convention: clbit 0 rightmost, registers split."""
        job = backend.run(backend.transpile(one_hot_circuit()), shots=SAMPLER_SHOTS)
        key, p = top(legacy_counts(job))
        assert key == "0 01", legacy_counts(job)
        assert p > MIN_FIDELITY - 0.1

    def test_result_header_matches_circuit(self, backend):
        """Result is named after the circuit and carries its metadata, as in Qiskit."""
        tc = backend.transpile(one_hot_circuit())
        res = backend.run(tc, shots=SAMPLER_SHOTS).result()
        header = res.results[0].header
        assert header["name"] == tc.name
        assert header["metadata"] == {"tag": 1}
        assert header["creg_sizes"] == [["a", 2], ["b", 1]]
        assert res.get_counts(tc) == res.get_counts()

    def test_reattached_job_keeps_registers(self, backend):
        """A job re-created from its id gets the registers, but not the name or metadata."""
        job = backend.run(backend.transpile(one_hot_circuit()), shots=SAMPLER_SHOTS)
        job.result()
        res = QJob(backend, int(job.job_id)).result()
        assert res.results[0].header["creg_sizes"] == [["a", 2], ["b", 1]]
        assert top(res.get_counts())[0] == "0 01"
        assert res.results[0].header["name"] == "circuit-0"

    def test_parameter_sweep_shape(self, backend):
        theta = Parameter("theta")
        qc = QuantumCircuit(1)
        qc.rx(theta, 0)
        qc.measure_all()
        values = [[0.0], [np.pi]]
        res = (
            QSamplerV2(backend)
            .run([(backend.transpile(qc), values)], shots=SAMPLER_SHOTS)
            .result()
        )
        meas = res[0].data.meas
        assert meas.shape == (2,)
        assert top(meas[0].get_counts())[0] == "0"
        assert top(meas[1].get_counts())[0] == "1"

    def test_pubs_with_different_shots(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = QSamplerV2(backend).run([(tc, None, 50), (tc, None, 80)])
        assert len(job.jobs) == 2
        assert [r.data.meas.num_shots for r in job.result()] == [50, 80]
        assert job.done()

    def test_backend_sampler_v2_agrees(self, backend):
        """qiskit BackendSamplerV2 works on a QaaS backend and reads the same bits."""
        tc = backend.transpile(one_hot_circuit())
        res = BackendSamplerV2(backend=backend).run([tc], shots=SAMPLER_SHOTS).result()
        assert top(res[0].data.a.get_counts())[0] == "01"
        assert top(res[0].data.b.get_counts())[0] == "0"


# ---------- 9. Exceptions ----------


class TestQExceptionContract:
    def test_hierarchy(self):
        assert issubclass(QAuthException, QException)
        assert issubclass(QException, Exception)

    def test_message_names_available_resources(self, provider):
        with pytest.raises(QException) as exc:
            provider.get_backend(INVALID_RESOURCE, BACKEND_NAME, **CLIENT_ARGS)
        assert INVALID_RESOURCE in str(exc.value) and "Available" in str(exc.value)


# ---------- 10. Former known issues (regression tests) ----------


class TestFixedIssues:
    def test_provider_list_available_backends(self):
        QProvider_ = type(make_provider(token="x"))
        backends = QProvider_.list_available_backends(
            TOKEN, PROJECT, lexis_resource=RESOURCE, lexis_api_url=LEXIS_API_URL
        )
        assert BACKEND_NAME in backends

    @pytest.mark.qpu
    @pytest.mark.timeout(JOB_TIMEOUT)
    def test_second_wait_does_not_hang(self, bell_job, monkeypatch):
        """Once the job is known to be finished, waiting again must not reopen the
        event stream (it never reports a state reached before it opened)."""
        assert bell_job._last_known_status in TERMINAL

        def no_stream(*args, **kwargs):
            raise AssertionError("waited on the event stream again")

        monkeypatch.setattr(bell_job._qclient, "iter_events", no_stream)
        assert bell_job._wait_for_terminal_state() == "FINISHED"
        assert bell_job._finished_artifacts(with_measurements=False)

    @pytest.mark.qpu
    @pytest.mark.timeout(JOB_TIMEOUT)
    def test_cancel_reaches_server(self, backend, client, ghz_circuit):
        job = backend.run(
            backend.transpile(ghz_circuit), shots=20000, job_name="qheappe_test_cancel"
        )
        try:
            assert job.cancel() is True
            assert job.status() == JobStatus.CANCELLED
            deadline = time.monotonic() + 60
            while (state := heappe_state(client, job.job_id)) not in TERMINAL:
                assert time.monotonic() < deadline, "no terminal state after cancel"
                time.sleep(2)
            assert state == "CANCELED"
        finally:
            client._run(
                client.cancel_job(int(job.job_id))
            )  # real cancel, save allocation
