"""
QaaS end-to-end integration tests.

Exercises the public client interface only:
  QProviderDev(token, project_name).get_backend(resource_name)
  QBackend.architecture / .transpile() / .run()
  QJob.result().get_counts()
  QException

Plus the lower-level Pulla path (circuit -> pulse schedule -> execution):
  QProviderDev.get_pulla() / .get_client()
  IQMClient.get_dynamic_architecture()
  Pulla.get_standard_compiler() / .fetch_default_calibration_set()
  qaas.qiskit_to_pulla() / qaas.sweep_job_to_qiskit()
  QPullaBackendIQM

Requires network access to LEXIS/HEAppE and a real IQM-backed resource.
Config via env vars so no secrets are hardcoded:
  QAAS_TOKEN             pre-obtained LEXIS access token
  QAAS_PROJECT           LEXIS project name
  QAAS_RESOURCE          accounting string, e.g. EQE1-CZ-P0001
  QAAS_INVALID_RESOURCE  known-bad resource name (for negative test)
  QAAS_INIT_QUEUE        Pulla init queue name (defaults to init_queue_test)
  QAAS_EXEC_QUEUE        Pulla execute queue name (defaults to compute_queue_test)

Auth: LEXIS login (LexisSession) is interactive and cannot be automated.
Obtain a token once via interactive login (or a service-account/offline-token
flow if LEXIS provides one) and pass it in via QAAS_TOKEN.

Run: pytest -v test_qaas_integration.py
"""

import json
import os
import time

import pytest
from qiskit import QuantumCircuit

from qaas import export_qasm3_with_custom_move, qiskit_to_pulla, sweep_job_to_qiskit
from qaas.client.provider import QProviderDev
from qaas.client.utils import QException

TOKEN = os.environ.get("QAAS_TOKEN")
PROJECT = os.environ.get("QAAS_PROJECT")
RESOURCE = os.environ.get("QAAS_RESOURCE")
INVALID_RESOURCE = os.environ.get("QAAS_INVALID_RESOURCE", "NON-EXISTENT-0000")
ADDITIONAL_BACKEND_ARGS = (
    json.loads(os.environ.get("ADDITIONAL_BACKEND_ARGS"))
    if os.environ.get("ADDITIONAL_BACKEND_ARGS", "") != ""
    else {}
)

INIT_QUEUE = os.environ.get("QAAS_INIT_QUEUE", "init_queue_test")
EXEC_QUEUE = os.environ.get("QAAS_EXEC_QUEUE", "compute_queue_test")

pytestmark = pytest.mark.skipif(
    not (TOKEN and PROJECT and RESOURCE),
    reason="QAAS_TOKEN / QAAS_PROJECT / QAAS_RESOURCE not set",
)


# ---------- fixtures ----------


@pytest.fixture(scope="session")
def token():
    # Login is interactive (LexisSession) and can't be automated here -
    # a valid token must be supplied out of band via QAAS_TOKEN.
    return TOKEN


@pytest.fixture(scope="session")
def provider(token):
    return QProviderDev(PROJECT, token=token)


@pytest.fixture(scope="session")
def backend(provider):
    return provider.get_backend(RESOURCE, **ADDITIONAL_BACKEND_ARGS)


@pytest.fixture
def bell_circuit():
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    return qc


@pytest.fixture
def ghz_circuit():
    qc = QuantumCircuit(3, 3)
    qc.h(0)
    qc.cx(0, 1)
    qc.cx(0, 2)
    qc.measure_all()
    return qc


# ---------- Pulla fixtures ----------


@pytest.fixture(scope="session")
def pulla(provider):
    return provider.get_pulla(RESOURCE, **ADDITIONAL_BACKEND_ARGS)


@pytest.fixture(scope="session")
def pulla_client(provider):
    return provider.get_client(RESOURCE, **ADDITIONAL_BACKEND_ARGS)


@pytest.fixture(scope="session")
def dynamic_architecture(pulla_client):
    return pulla_client.get_dynamic_architecture()


@pytest.fixture(scope="session")
def pulla_compiler(pulla):
    return pulla.get_standard_compiler()


@pytest.fixture(scope="session")
def pulla_backend(pulla):
    return pulla.get_qbackend()


# ---------- 1. Auth / provider ----------


class TestProvider:
    def test_provider_init_valid_token(self, token):
        QProviderDev(PROJECT, token=token)

    def test_provider_init_invalid_token_raises(self):
        with pytest.raises(QException):
            bad_provider = QProviderDev("not-a-valid-token", PROJECT)
            bad_provider.get_backend(RESOURCE, **ADDITIONAL_BACKEND_ARGS)

    def test_provider_init_unknown_project_raises(self, token):
        with pytest.raises(QException):
            bad_provider = QProviderDev(token, "definitely-not-a-real-project")
            bad_provider.get_backend(RESOURCE, **ADDITIONAL_BACKEND_ARGS)


# ---------- 2. Backend retrieval ----------


class TestGetBackend:
    def test_get_backend_valid_resource(self, provider):
        backend = provider.get_backend(RESOURCE, **ADDITIONAL_BACKEND_ARGS)
        assert backend is not None

    def test_get_backend_invalid_resource_raises(self, provider):
        with pytest.raises(QException):
            provider.get_backend(INVALID_RESOURCE, **ADDITIONAL_BACKEND_ARGS)

    def test_get_backend_empty_resource_raises(self, provider):
        with pytest.raises((QException, ValueError)):
            provider.get_backend("", **ADDITIONAL_BACKEND_ARGS)


# ---------- 3. Architecture ----------


class TestArchitecture:
    def test_architecture_has_qubits(self, backend):
        assert len(backend.architecture.qubits) > 0

    def test_architecture_has_gates(self, backend):
        assert len(backend.architecture.gates) > 0

    def test_architecture_stable_across_calls(self, backend):
        a1 = backend.architecture.qubits
        a2 = backend.architecture.qubits
        assert list(a1) == list(a2)


# ---------- 4. Transpile ----------


class TestTranspile:
    def test_transpile_returns_circuit(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        assert tc is not None
        # assert tc.num_qubits == bell_circuit.num_qubits
        assert tc.num_qubits > 0

    @pytest.mark.xfail
    def test_transpile_uses_native_gate_set(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        allowed = set(backend.architecture.gates.keys())
        used = {instr.operation.name for instr in tc.data}
        # measurement/barrier ops are not "gates" in the architecture dict
        used -= {"measure", "barrier"}
        assert used.issubset(allowed), f"Unsupported gates emitted: {used - allowed}"

    def test_transpile_optimize_single_qubits_flag(self, backend, bell_circuit):
        default = backend.transpile(bell_circuit)
        optimized = backend.transpile(bell_circuit, optimize_single_qubits=True)
        assert optimized is not None
        assert len(optimized.data) <= len(default.data)

    def test_transpile_empty_circuit(self, backend):
        qc = QuantumCircuit(1, 1)
        tc = backend.transpile(qc)
        assert tc is not None

    @pytest.mark.xfail
    def test_transpile_invalid_input_raises(self, backend):
        with pytest.raises((QException, TypeError, ValueError)):
            backend.transpile("not a circuit")

    @pytest.mark.xfail
    def test_transpile_exceeds_qubit_count_raises(self, backend):
        too_wide = QuantumCircuit(len(backend.architecture.qubits) + 100)
        with pytest.raises(QException):
            backend.transpile(too_wide)


# ---------- 5. Run / job lifecycle ----------


class TestRunAndJob:
    def test_run_returns_job_handle(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc, shots=100)
        assert job is not None

    def test_job_result_blocks_and_returns(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc, shots=100)
        result = job.result()
        assert result is not None

    def test_get_counts_shape(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc, shots=1000)
        counts = job.result().get_counts()
        assert isinstance(counts, dict)
        assert sum(counts.values()) == 1000
        for bitstring in counts:
            assert set(bitstring) <= {"0", "1"}

    def test_bell_state_correlation(self, backend, bell_circuit):
        """Physical sanity check: Bell state -> mostly 00/11, correlated bits."""
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc, shots=2000)
        counts = job.result().get_counts()
        correlated = sum(v for k, v in counts.items() if k in ("00", "11"))
        assert correlated / sum(counts.values()) > 0.85  # allow hardware noise

    def test_default_shots_value(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc)  # default shots=1000
        counts = job.result().get_counts()
        assert sum(counts.values()) == 1000

    @pytest.mark.xfail
    def test_shots_zero_raises(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        with pytest.raises((QException, ValueError)):
            backend.run(tc, shots=0)

    @pytest.mark.xfail
    def test_untranspiled_circuit_raises_on_run(self, backend, bell_circuit):
        with pytest.raises(QException):
            backend.run(bell_circuit, shots=100)

    def test_multiple_jobs_are_independent(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job1 = backend.run(tc, shots=100)
        job2 = backend.run(tc, shots=100)
        r1, r2 = job1.result(), job2.result()
        assert r1.get_counts() is not r2.get_counts()

    def test_result_is_idempotent(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        job = backend.run(tc, shots=100)
        c1 = job.result().get_counts()
        c2 = job.result().get_counts()
        assert c1 == c2


# ---------- 6. OpenQASM3 input path ----------


class TestQasmInput:
    def test_run_accepts_transpiled_qasm3_string(self, backend, bell_circuit):
        qasm = export_qasm3_with_custom_move(backend.transpile(bell_circuit))
        job = backend.run([qasm], shots=100)
        counts = job.result().get_counts()
        assert sum(counts.values()) == 100

    @pytest.mark.xfail
    def test_invalid_qasm_string_raises(self, backend):
        with pytest.raises(QException):
            backend.run(["not valid qasm"], shots=100)


# ---------- 7. Concurrency / throughput smoke test ----------


class TestConcurrency:
    def test_sequential_jobs_within_timeout(self, backend, bell_circuit):
        tc = backend.transpile(bell_circuit)
        start = time.time()
        for _ in range(3):
            backend.run(tc, shots=50).result()
        elapsed = time.time() - start
        assert elapsed < 300, (
            f"3 sequential jobs took {elapsed:.1f}s, exceeds 5 min budget"
        )


# ---------- 8. QException contract ----------


class TestQException:
    def test_qexception_is_catchable_as_exception(self):
        assert issubclass(QException, Exception)

    def test_qexception_message_propagates(self, provider):
        try:
            provider.get_backend(INVALID_RESOURCE, **ADDITIONAL_BACKEND_ARGS)
        except QException as e:
            assert str(e) != ""
        else:
            pytest.fail("expected QException for invalid resource")


# ---------- 9. Pulla low-level path ----------
# Circuit -> pulse schedule compilation and execution, bypassing QBackend.run().
# Mirrors the flow from the IQM Pulla tutorial notebook.


class TestPullaSetup:
    def test_get_pulla_returns_instance(self, provider):
        p = provider.get_pulla(RESOURCE, **ADDITIONAL_BACKEND_ARGS)
        assert p is not None

    def test_get_client_returns_instance(self, provider):
        client = provider.get_client(RESOURCE, **ADDITIONAL_BACKEND_ARGS)
        assert client is not None

    def test_dynamic_architecture_has_calibration_set(self, dynamic_architecture):
        assert dynamic_architecture.calibration_set_id is not None

    def test_pulla_qbackend_matches_dynamic_architecture(
        self, pulla, dynamic_architecture
    ):
        pulla_backend_from_p = pulla.get_qbackend()
        assert pulla_backend_from_p is not None
        # calibration set used by the quick backend should track the dynamic architecture
        assert (
            pulla_backend_from_p._calibration_set_id
            == dynamic_architecture.calibration_set_id
        )

    def test_standard_compiler_creation(self, pulla_compiler):
        assert pulla_compiler is not None

    def test_qpulla_backend_iqm_creation(self, pulla_backend):
        assert pulla_backend is not None
        assert pulla_backend.name


class TestPullaCompileAndRun:
    def test_qiskit_to_pulla_conversion(self, pulla, pulla_backend, bell_circuit):
        qc_transpiled = pulla.get_qbackend().transpile(
            bell_circuit,
            layout_method="sabre",
            optimization_level=0,
            optimize_single_qubits=True,
        )
        circuits, compiler = qiskit_to_pulla(pulla, pulla_backend, [qc_transpiled])
        assert len(circuits) == 1
        assert compiler is not None

    def test_compile_produces_run_definition(self, pulla, pulla_backend, bell_circuit):
        qc_transpiled = pulla.get_qbackend().transpile(
            bell_circuit,
            layout_method="sabre",
            optimization_level=0,
            optimize_single_qubits=True,
        )
        circuits, compiler = qiskit_to_pulla(pulla, pulla_backend, [qc_transpiled])
        settings = compiler.get_settings(circuits[0])
        settings.set_shots(100)
        run_definition, context = compiler.compile(circuits, settings=settings)
        assert run_definition is not None
        assert "readout_metrics" in context

    def test_submit_playlist_bell_state(self, pulla, pulla_backend, bell_circuit):
        shots = 200
        qc_transpiled = pulla.get_qbackend().transpile(
            bell_circuit,
            layout_method="sabre",
            optimization_level=0,
            optimize_single_qubits=True,
        )
        circuits, compiler = qiskit_to_pulla(pulla, pulla_backend, [qc_transpiled])
        settings = compiler.get_settings(circuits[0])
        settings.set_shots(shots)
        run_definition, context = compiler.compile(circuits, settings=settings)

        job = pulla.submit_playlist(run_definition, context=context)
        assert job.job_id is not None
        job.wait_for_completion()

        qiskit_result = sweep_job_to_qiskit(job, shots=shots)
        counts = qiskit_result.get_counts()
        assert sum(counts.values()) == shots
        correlated = sum(v for k, v in counts.items() if k in ("00", "11"))
        assert correlated / shots > 0.85

    def test_submit_playlist_ghz_state(self, pulla, pulla_backend, ghz_circuit):
        shots = 200
        qc_transpiled = pulla_backend.transpile(
            ghz_circuit,
            layout_method="sabre",
            optimization_level=3,
        )
        circuits, compiler = qiskit_to_pulla(pulla, pulla_backend, [qc_transpiled])
        settings = compiler.get_settings(circuits[0])
        settings.set_shots(shots)
        run_definition, context = compiler.compile(circuits, settings=settings)

        job = pulla.submit_playlist(run_definition, context=context)
        job.wait_for_completion()

        counts = sweep_job_to_qiskit(job, shots=shots).get_counts()
        correlated = sum(v for k, v in counts.items() if k in ("000", "111"))
        assert correlated / shots > 0.75  # 3-qubit GHZ, allow more noise

    def test_qbackend_run_via_pulla_backend(self, pulla_backend, bell_circuit):
        """QPullaBackendIQM should behave like a standard QBackend for run()."""
        tc = pulla_backend.transpile(
            bell_circuit,
            layout_method="sabre",
            optimization_level=3,
        )
        job = pulla_backend.run(tc, shots=100)
        counts = job.result().get_counts()
        assert sum(counts.values()) == 100


class TestParameterizedCircuitExecution:
    """Test execution of parameterized Qiskit circuits on an IQM backend."""

    def test_parameterized_circuit_sweep(self, backend):
        import numpy as np
        from qiskit.circuit import Parameter
        from qiskit.compiler import transpile

        theta = Parameter("theta")
        qc = QuantumCircuit(2, 2)
        qc.ry(theta, 0)
        qc.cx(0, 1)
        qc.measure_all()

        for theta_value in (0, np.pi / 4, np.pi / 2, np.pi):
            bound_circuit = qc.assign_parameters({theta: theta_value})
            transpiled_circuit = transpile(
                bound_circuit,
                backend=backend,
                optimization_level=2,
            )

            job = backend.run(transpiled_circuit, shots=50)
            counts = job.result().get_counts()

            assert sum(counts.values()) == 50

    def test_zero_rotation_prepares_zero_state(self, backend):
        """Verify that RY(0) followed by CX produces |00⟩, apart from hardware errors."""
        from qiskit.circuit import Parameter
        from qiskit.compiler import transpile

        theta = Parameter("theta")
        qc = QuantumCircuit(2, 2)
        qc.ry(theta, 0)
        qc.cx(0, 1)
        qc.measure_all()

        bound_circuit = qc.assign_parameters({theta: 0})
        transpiled_circuit = transpile(
            bound_circuit,
            backend=backend,
            optimization_level=2,
        )

        counts = (
            backend.run(
                transpiled_circuit,
                shots=200,
            )
            .result()
            .get_counts()
        )

        assert counts.get("00", 0) / 200 > 0.85
