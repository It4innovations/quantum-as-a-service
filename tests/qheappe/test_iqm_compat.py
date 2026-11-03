"""Offline tests for IQM stack selection (qheappe_client.iqm_compat).

They run against whichever IQM stack is installed (``qaas[iqm_qiskit]`` or
``qaas[iqm35_qiskit]``); run the suite in both environments to cover both adapters.
"""

import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from iqm.iqm_client import DynamicQuantumArchitecture
from qiskit import QuantumCircuit

from qheappe_client.backend_metadata import (
    LexisProject,
    LexisResource,
    QBackendMetadata,
    QCluster,
    QMachineInfo,
)
from qheappe_client.iqm_compat import (
    CLIENT_MAJOR_TO_STACK,
    STATION_CONTROL_MAJOR_TO_STACK,
    IQMStack,
    QBackendIQMFactory,
    installed_stack,
    required_stack,
)
from qheappe_client.iqm_compat import stack as stack_mod
from qheappe_client.qiskit.qbackend_iqm import QBackendIQM
from qheappe_client.utils import QException

INSTALLED = installed_stack()
OTHER = IQMStack.IQM35 if INSTALLED is IQMStack.IQM36 else IQMStack.IQM36
# station-control major that each stack talks to
SC_MAJOR = {s: m for m, s in STATION_CONTROL_MAJOR_TO_STACK.items()}


def machine_info(station_control_version):
    version = {"qscheduler_version": "1.0.0"}
    if station_control_version is not None:
        version["station_control_version"] = station_control_version
    return QMachineInfo.from_response({"type": "iqm", "version": version})


def dqa() -> DynamicQuantumArchitecture:
    def gate(impl, loci):
        return {
            "implementations": {impl: {"loci": loci}},
            "default_implementation": impl,
            "override_default_implementation": {},
        }

    return DynamicQuantumArchitecture.model_validate({
        "calibration_set_id": str(uuid4()),
        "qubits": ["QB1", "QB2"],
        "computational_resonators": [],
        "gates": {
            "prx": gate("drag_gaussian", [["QB1"], ["QB2"]]),
            "cz": gate("tgss", [["QB1", "QB2"]]),
            "measure": gate("constant", [["QB1"], ["QB2"]]),
        },
    })


def metadata(station_control_version) -> QBackendMetadata:
    cl = QCluster(name="VLQ", heappe_url="https://h1",
                  machine_info=machine_info(station_control_version), available=True)
    return QBackendMetadata(LexisProject("P"), LexisResource("R", "id", "-", "-", {"VLQ": cl}),
                            cl, dynamic_architecture=dqa())


class TestStackDetection:
    def test_installed_stack_matches_iqm_client(self):
        from importlib.metadata import version

        assert CLIENT_MAJOR_TO_STACK[int(version("iqm-client").split(".")[0])] is INSTALLED

    @pytest.mark.parametrize(("reported", "expected"), [
        ("13.0.3", IQMStack.IQM35),
        ("v13.1", IQMStack.IQM35),
        ("14.0.0", IQMStack.IQM36),
        ("14.2.0 (local editable)", IQMStack.IQM36),
        ("99.0.0", None),
        ("garbage", None),
        (None, None),
    ])
    def test_required_stack(self, reported, expected):
        assert required_stack(machine_info(reported)) is expected

    def test_required_stack_without_machine_info(self):
        assert required_stack(None) is None

    def test_missing_iqm_client(self, monkeypatch):
        monkeypatch.setattr(stack_mod, "installed_iqm_client_version", lambda: None)
        with pytest.raises(QException, match="iqm_qiskit"):
            stack_mod.installed_stack()

    def test_unsupported_iqm_client(self, monkeypatch):
        monkeypatch.setattr(stack_mod, "installed_iqm_client_version", lambda: "30.0.0")
        with pytest.raises(QException, match="not supported"):
            stack_mod.installed_stack()


class TestResolveStack:
    def test_match(self):
        mi = machine_info(f"{SC_MAJOR[INSTALLED]}.0.0")
        assert QBackendIQMFactory.resolve_stack(mi) is INSTALLED

    def test_mismatch_raises_with_install_hint(self):
        mi = machine_info(f"{SC_MAJOR[OTHER]}.0.0")
        extra = stack_mod.STACK_EXTRA[OTHER]
        with pytest.raises(QException, match=rf"qaas\[{extra}\]"):
            QBackendIQMFactory.resolve_stack(mi, backend_name="VLQ")

    def test_mismatch_non_strict_warns(self, caplog):
        mi = machine_info(f"{SC_MAJOR[OTHER]}.0.0")
        with caplog.at_level(logging.WARNING):
            assert QBackendIQMFactory.resolve_stack(mi, strict=False) is INSTALLED
        assert "strict=False" in caplog.text

    def test_unknown_server_version_uses_installed(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert QBackendIQMFactory.resolve_stack(machine_info(None)) is INSTALLED
        assert "cannot tell" in caplog.text


class TestFactory:
    def test_backend_class(self):
        cls = QBackendIQMFactory.backend_class()
        assert issubclass(cls, QBackendIQM) and cls.iqm_stack is INSTALLED

    def test_adapter_exposes_common_api(self):
        adapter = QBackendIQMFactory.adapter()
        for name in ("STACK", "JobStatus", "HERALDING_KEY", "sweep_job_to_qiskit", "qiskit_to_pulla"):
            assert hasattr(adapter, name), name
        assert adapter.STACK is INSTALLED

    def test_other_adapter_not_importable(self):
        with pytest.raises(QException, match=stack_mod.STACK_EXTRA[OTHER]):
            QBackendIQMFactory.adapter(OTHER)

    def test_create_and_build_job_definition(self):
        md = metadata(f"{SC_MAJOR[INSTALLED]}.0.0")
        backend = QBackendIQMFactory.create(SimpleNamespace(), md)
        assert type(backend).iqm_stack is INSTALLED
        assert backend.num_qubits == 2

        qc = QuantumCircuit(2, 2)
        qc.h(0)
        qc.cx(0, 1)
        qc.measure([0, 1], [0, 1])
        job_def = backend.create_circuit_job_definition(backend.transpile(qc), shots=10)
        assert job_def.shots == 10 and len(job_def.circuits) == 1
        assert str(job_def.calibration_set_id) == str(md.dynamic_architecture.calibration_set_id)
        # 35.x name still works
        assert backend.create_run_request(backend.transpile(qc), shots=5).shots == 5

    def test_create_rejects_mismatched_server(self):
        with pytest.raises(QException, match="needs the"):
            QBackendIQMFactory.create(SimpleNamespace(), metadata(f"{SC_MAJOR[OTHER]}.0.0"))

    def test_create_requires_dynamic_architecture(self):
        md = metadata(f"{SC_MAJOR[INSTALLED]}.0.0")
        md._dynamic_architecture = None
        with pytest.raises(ValueError, match="dynamic architecture"):
            QBackendIQMFactory.create(SimpleNamespace(), md)
