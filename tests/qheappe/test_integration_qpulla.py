"""Live tests of pulse-level access (QPulla) against LEXIS / HEAppE / QScheduler v0.2.2+.

Needs the IQM 36 stack, a machine with pulse access enabled in QScheduler, and
HEAppE 6.7.0+.

Tests marked ``qpu`` submit a pulse-level job and consume allocation.

Run: pytest tests/qheappe/test_integration_qpulla.py -m integration -v
"""

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import pytest
from settings import BACKEND_NAME, CLIENT_ARGS, JOB_TIMEOUT, LIVE, RESOURCE, SKIP_REASON

pytest.importorskip("iqm.cpc", reason="QPulla needs the IQM 36 stack")

from exa.common.data.parameter import Sweep
from iqm.models.playlist.waveforms import Waveform
from iqm.pulse.gates.prx import PRX_CustomWaveforms
from iqm.station_control.client.serializers import serialize_run_definition
from iqm.station_control.interface.models import RunDefinition
from iqm.station_control.interface.models.jobs_new import JobStatus
from qiskit import QuantumCircuit

from qheappe_client import QPulla
from qheappe_client.qclient import MachineArtifactIQM

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not LIVE, reason=SKIP_REASON)]

SHOTS = 200


@pytest.fixture(scope="module")
def pulla(provider) -> QPulla:
    return provider.get_pulla(RESOURCE, BACKEND_NAME, **CLIENT_ARGS)


class TestStationData:
    def test_get_pulla(self, pulla, backend):
        assert isinstance(pulla, QPulla)
        assert pulla.quantum_computer_name == backend._backend_metadata.backend_name
        assert pulla.backend.client is pulla

    @pytest.mark.parametrize("name", list(MachineArtifactIQM))
    def test_machine_artifacts(self, pulla, name):
        res = pulla.qclient.get_machine_artifact(name)
        assert res.body

    def test_chip_label_and_topology(self, pulla):
        assert pulla.get_chip_label()
        assert pulla.get_chip_topology().qubits

    def test_settings(self, pulla):
        assert pulla.executor.get_settings() is not None

    def test_channel_properties(self, pulla):
        assert pulla.executor.get_channel_properties()

    def test_static_architecture(self, pulla, backend):
        sqa = pulla.get_static_quantum_architecture()
        assert set(backend.architecture.qubits) <= set(sqa.qubits)

    def test_calibration_set(self, pulla, backend):
        cs = pulla.get_calibration_set()
        assert cs.observations
        assert cs.observation_set_id == backend.architecture.calibration_set_id

    def test_dynamic_architecture(self, pulla, backend):
        dqa = pulla.get_dynamic_quantum_architecture()
        assert dqa.qubits == backend.architecture.qubits

    def test_standard_compiler(self, pulla):
        compiler = pulla.get_standard_compiler()
        assert compiler.dut_label == pulla.get_chip_label()


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestPulseJob:
    def test_bell_circuit_at_pulse_level(self, pulla, bell_circuit):
        circuits, compiler = pulla.qiskit_to_iqm(pulla.backend.transpile(bell_circuit))
        settings = compiler.get_settings(circuits=circuits)
        settings.set_shots(SHOTS)
        run_definition, context = compiler.compile(circuits, settings=settings)

        job = pulla.submit_playlist(run_definition, context=context)
        status = job.wait_for_completion(timeout_secs=JOB_TIMEOUT, polling_interval=2)
        assert status is JobStatus.COMPLETED, job.job_data.messages

        counts = pulla.sweep_job_to_qiskit(job, shots=SHOTS).get_counts()
        assert sum(counts.values()) == SHOTS


# ---------- custom gate (examples/qheappe_client/pulse_level_custom_x_gate.ipynb) ----------

CUSTOM_IMPL = "hann_drag_test"
SWEEP_POINTS = 5


@dataclass(frozen=True)
class HannEnvelope(Waveform):
    def _sample(self, sample_coords: np.ndarray) -> np.ndarray:
        return np.cos(np.pi * sample_coords) ** 2


@dataclass(frozen=True)
class HannDerivative(Waveform):
    def _sample(self, sample_coords: np.ndarray) -> np.ndarray:
        return -np.sin(2 * np.pi * sample_coords)


class HannDragPRX(PRX_CustomWaveforms, wave_i=HannEnvelope, wave_q=HannDerivative):
    """PRX gate made of one Hann pulse, as in the custom gate example."""


class CustomGate(NamedTuple):
    compiler: object
    qubit: str
    native_impl: str
    duration: float
    amplitude: float


def x_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(1, 1, name="x")
    qc.x(0)
    qc.measure(0, 0)
    return qc


@pytest.fixture(scope="module")
def custom_gate(pulla) -> CustomGate:
    """Qiskit compiler with the custom gate registered, and the native gate's calibration."""
    compiler = pulla.get_qiskit_compiler()
    compiler.add_implementation("prx", CUSTOM_IMPL, HannDragPRX, overwrite=True)
    qubit = pulla.get_chip_topology().qubits_sorted[0]
    settings = compiler.get_settings(circuits=[x_circuit()])
    native_impl = settings.gate_definitions.prx.default_implementation.value
    native = settings.gates.prx[native_impl][qubit]
    amplitude = float(native.amplitude_i.value)
    return CustomGate(
        compiler,
        qubit,
        native_impl,
        float(native.duration.value),
        amplitude if 0 < amplitude < 1 else 0.3,
    )


def custom_settings(gate: CustomGate, circuit, amplitude: float, shots: int = SHOTS):
    """Settings that use the custom gate as the default prx implementation."""
    s = gate.compiler.get_settings(circuits=[circuit])
    s.gate_definitions.prx.default_implementation = CUSTOM_IMPL
    s.gates.prx[CUSTOM_IMPL][gate.qubit].set_from_dict(
        {"duration": gate.duration, "amplitude_i": amplitude, "amplitude_q": 0.0}
    )
    s.set_shots(shots)
    return s


def amplitude_sweep(gate: CustomGate, settings) -> tuple[np.ndarray, list]:
    amplitudes = np.linspace(0.5, 1.5, SWEEP_POINTS) * gate.amplitude
    node = settings.gates.prx[CUSTOM_IMPL][gate.qubit]
    sweep = (
        Sweep(parameter=node.amplitude_i.parameter, data=amplitudes),
        Sweep(parameter=node.amplitude_q.parameter, data=0.0 * amplitudes),
    )
    return amplitudes, [sweep]


def compile_custom(gate: CustomGate, circuit, settings, sweeps=None):
    return gate.compiler.compile(
        circuits=[circuit],
        components=[gate.qubit],
        settings=settings,
        sweeps=sweeps or [],
    )


def excited_probability(result, qubit: str) -> np.ndarray:
    ds = result.dataset
    names = [
        n
        for n in ds.data_vars
        if n.startswith(f"{qubit}__") and n.endswith("excited_state_probability")
    ]
    assert names, f"no excited state probability for {qubit}: {list(ds.data_vars)}"
    return np.atleast_1d(np.asarray(ds[names[0]].values, dtype=float))


class TestCustomGateCompile:
    """Custom pulse shape against live station data; compiles only, submits nothing."""

    def test_registered(self, custom_gate):
        prx = custom_gate.compiler.gate_definitions["prx"]
        assert CUSTOM_IMPL in prx.implementations
        assert custom_gate.native_impl != CUSTOM_IMPL
        assert custom_gate.duration > 0

    def test_settings_node(self, custom_gate):
        s = custom_settings(custom_gate, x_circuit(), custom_gate.amplitude)
        node = s.gates.prx[CUSTOM_IMPL][custom_gate.qubit]
        assert node.amplitude_i.value == pytest.approx(custom_gate.amplitude)
        assert node.duration.value == pytest.approx(custom_gate.duration)

    def test_compile_single_x(self, custom_gate):
        circuit = x_circuit()
        s = custom_settings(custom_gate, circuit, custom_gate.amplitude)
        run_definition, context = compile_custom(custom_gate, circuit, s)
        assert isinstance(run_definition, RunDefinition)
        assert context
        # what HEAppEExecutor sends as the pulse task payload
        assert serialize_run_definition(run_definition).SerializeToString()

    def test_compile_amplitude_sweep(self, custom_gate):
        circuit = x_circuit()
        s = custom_settings(custom_gate, circuit, custom_gate.amplitude)
        amplitudes, sweeps = amplitude_sweep(custom_gate, s)
        run_definition, _ = compile_custom(custom_gate, circuit, s, sweeps)

        # Gate parameters are hard sweeps: every point is compiled into the playlist,
        # and RunDefinition.hard_sweeps maps each return parameter to them
        # (sweep_definition.sweeps holds only soft sweeps over controller settings).
        assert run_definition.hard_sweeps
        for return_parameter, nd_sweep in run_definition.hard_sweeps.items():
            swept = [sw for parallel in nd_sweep for sw in parallel]
            amp_i = [sw for sw in swept if "amplitude_i" in str(sw.parameter.name)]
            assert amp_i, (return_parameter, [str(sw.parameter.name) for sw in swept])
            np.testing.assert_allclose(
                np.asarray(amp_i[0].data, dtype=float), amplitudes
            )
        assert serialize_run_definition(run_definition).SerializeToString()


@pytest.mark.qpu
@pytest.mark.timeout(JOB_TIMEOUT)
class TestCustomGateJob:
    def test_rabi_amplitude_sweep(self, pulla, custom_gate):
        circuit = x_circuit()
        s = custom_settings(custom_gate, circuit, custom_gate.amplitude)
        _, sweeps = amplitude_sweep(custom_gate, s)
        run_definition, context = compile_custom(custom_gate, circuit, s, sweeps)

        job = pulla.submit_playlist(run_definition, context=context)
        status = job.wait_for_completion(timeout_secs=JOB_TIMEOUT, polling_interval=2)
        assert status is JobStatus.COMPLETED, job.job_data.messages

        p1 = excited_probability(
            job.result_exa(compiler=custom_gate.compiler), custom_gate.qubit
        )
        assert p1.shape == (SWEEP_POINTS,)
        assert np.all((p1 >= 0) & (p1 <= 1))
