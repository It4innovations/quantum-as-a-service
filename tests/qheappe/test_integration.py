"""Live end-to-end tests for qheappe_client against LEXIS / HEAppE QScheduler.

Public surface exercised:
  QProvider(lexis_project, token, lexis_api_url).get_backend / get_client / ...
  QBackendIQM.architecture / .transpile() / .create_run_request() / .run()
  QJob.result() (PrimitiveResult and legacy Result), QJob.status(), QJob.cancel()
  QClient sync and async API

Tests marked `qpu` submit jobs and consume allocation. Tests marked `known_issue`
document confirmed defects (xfail); deselect them with  -m "not known_issue".
QPulla is not implemented yet and is not covered.

Run: pytest -m integration -v
"""

import asyncio
import time
import warnings
from collections import Counter

import numpy as np
import pytest
from conftest import close_provider, make_provider
from qiskit import QuantumCircuit
from qiskit.circuit import Parameter
from qiskit.compiler import transpile as qiskit_transpile
from qiskit.providers.jobstatus import JobStatus
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

from qheappe_client import QBackendIQM, QBackendMetadata
from qheappe_client.qclient import QClient
from qheappe_client.qiskit.qbackend import QJob
from qheappe_client.utils import HEAppEJobState, QAuthException, QException

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not LIVE, reason=SKIP_REASON)]

TERMINAL = QClient.TERMINAL_JOB_STATES
# Qiskit op names produced by the IQM transpiler (prx -> "r").
NATIVE_OPS = {"r", "cz", "move", "measure", "barrier", "delay", "reset", "id"}


def legacy_counts(job: QJob) -> dict[str, int]:
    return job.result(qiskit_legacy=True).get_counts()


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

    @pytest.mark.xfail(strict=True, raises=TypeError,
                       reason="run() requires shots; Options(shots=...) is not used as default")
    def test_run_without_shots_uses_default(self, backend, bell_circuit):
        backend.run(backend.transpile(bell_circuit))


# ---------- 8. Job lifecycle and results ----------

BELL_SHOTS = 1000


@pytest.fixture(scope="module")
def bell_job(backend):
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    job = backend.run(backend.transpile(qc), shots=BELL_SHOTS, job_name="qheappe_test_bell")
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
        assert heappe_state(client, bell_job.job_id) == "FINISHED"

    def test_result_cached(self, bell_job):
        assert bell_job.result() is bell_job.result()

    def test_legacy_counts_shape(self, backend, bell_job):
        counts = legacy_counts(reattach(backend, bell_job))
        assert sum(counts.values()) == BELL_SHOTS
        assert all(set(k) <= {"0", "1"} and len(k) == 2 for k in counts)

    def test_primitive_result_shape(self, backend, bell_job):
        res = reattach(backend, bell_job).result()
        meas = res[0].data.meas
        assert meas.num_shots == BELL_SHOTS
        assert sum(meas.get_counts().values()) == BELL_SHOTS

    def test_legacy_memory_matches_counts(self, backend, bell_job):
        res = reattach(backend, bell_job).result(qiskit_legacy=True, with_measurements=True)
        memory = res.get_memory()
        assert len(memory) == BELL_SHOTS
        assert Counter(memory) == res.get_counts()

    def test_primitive_bitstrings_match_counts(self, backend, bell_job):
        meas = reattach(backend, bell_job).result(with_measurements=True)[0].data.meas
        bitstrings = meas.get_bitstrings()
        assert len(bitstrings) == BELL_SHOTS
        assert Counter(bitstrings) == meas.get_counts()

    def test_key_order_reverses_bitstrings(self, backend, bell_job):
        rtl = reattach(backend, bell_job).result(qiskit_legacy=True).get_counts()
        ltr = reattach(backend, bell_job).result(
            qiskit_legacy=True, key_order_left_to_right=True
        ).get_counts()
        assert ltr == {k[::-1]: v for k, v in rtl.items()}

    def test_counts_identical_across_formats(self, backend, bell_job):
        legacy = legacy_counts(reattach(backend, bell_job))
        primitive = reattach(backend, bell_job).result()[0].data.meas.get_counts()
        assert legacy == primitive

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
        assert len(res) == 2
        assert [r.data.meas.num_bits for r in res] == [2, 3]

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
            tc = qiskit_transpile(qc.assign_parameters({theta: value}),
                                  backend=backend, optimization_level=2)
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


# ---------- 9. Exceptions ----------


class TestQExceptionContract:
    def test_hierarchy(self):
        assert issubclass(QAuthException, QException)
        assert issubclass(QException, Exception)

    def test_message_names_available_resources(self, provider):
        with pytest.raises(QException) as exc:
            provider.get_backend(INVALID_RESOURCE, BACKEND_NAME, **CLIENT_ARGS)
        assert INVALID_RESOURCE in str(exc.value) and "Available" in str(exc.value)


# ---------- 10. Known issues (document defects; xfail) ----------

#TODO: Verify following issues. Some of them may be already solved.

@pytest.mark.known_issue
class TestKnownIssues:
    @pytest.mark.xfail(strict=True, raises=QAuthException,
                       reason="list_available_backends never connects the QClient")
    def test_provider_list_available_backends(self):
        QProvider_ = type(make_provider(token="x"))
        QProvider_.list_available_backends(TOKEN, PROJECT, lexis_resource=RESOURCE,
                                           lexis_api_url=LEXIS_API_URL)

    @pytest.mark.qpu
    @pytest.mark.timeout(JOB_TIMEOUT)
    @pytest.mark.xfail(strict=True,
                       reason="QJob._submitted_job keeps the submit-time State, so a second "
                              "result() with another format waits on the event stream forever")
    def test_submitted_job_state_refreshed_after_result(self, bell_job):
        state = HEAppEJobState.readable(bell_job._submitted_job["State"])
        assert state in TERMINAL

    @pytest.mark.qpu
    @pytest.mark.timeout(JOB_TIMEOUT)
    @pytest.mark.xfail(strict=False,
                       reason="QJob.cancel() does not await QClient.cancel_job; "
                              "status() references JobStatus.CANCELED")
    def test_cancel_reaches_server(self, backend, client, ghz_circuit):
        job = backend.run(backend.transpile(ghz_circuit), shots=20000,
                          job_name="qheappe_test_cancel")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                job.cancel()
            assert job.status() == JobStatus.CANCELLED
            deadline = time.monotonic() + 60
            while (state := heappe_state(client, job.job_id)) not in TERMINAL:
                assert time.monotonic() < deadline, "no terminal state after cancel"
                time.sleep(2)
            assert state == "CANCELED"
        finally:
            client._run(client.cancel_job(int(job.job_id)))  # real cancel, save allocation
