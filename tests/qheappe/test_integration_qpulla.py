"""Live tests of pulse-level access (QPulla) against LEXIS / HEAppE / QScheduler v0.2.2+.

Needs the IQM 36 stack, a machine with pulse access enabled in QScheduler, and
HEAppE 6.7.0+.

Tests marked ``qpu`` submit a pulse-level job and consume allocation.

Run: pytest tests/qheappe/test_integration_qpulla.py -m integration -v
"""

import pytest
from settings import BACKEND_NAME, CLIENT_ARGS, JOB_TIMEOUT, LIVE, RESOURCE, SKIP_REASON

pytest.importorskip("iqm.cpc", reason="QPulla needs the IQM 36 stack")

from iqm.station_control.interface.models.jobs_new import JobStatus

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
