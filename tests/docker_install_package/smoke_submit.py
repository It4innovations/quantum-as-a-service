"""Smoke test of a qaas installation (mirrors randomnumbers_Qprovider.ipynb).

Env vars (from .env_install_test):
  ACCESS_TOKEN   LEXIS access token
  LEXIS_PROJECT  LEXIS project name
  RESOURCE_NAME  resource name, e.g. VLQ-EU
  SHOTS          optional, default 100
"""

import os
import sys
from importlib.metadata import version
from time import time

os.environ.setdefault("QPROVIDER_LOGLEVEL", "INFO")


def env(name: str) -> str:
    value = os.environ.get(name) or os.environ.get(name.lower())
    if not value:
        sys.exit(f"Missing env var {name}")
    return value


token = env("ACCESS_TOKEN")
lexis_project = env("LEXIS_PROJECT")
resource_name = env("RESOURCE_NAME")
shots = int(os.environ.get("SHOTS", "100"))

from qiskit import QuantumCircuit

import qaas
from qaas import export_qasm3_with_custom_move as qasm3dumps
from qaas.client import QBackend, QJob, QProvider
from qaas.client.backend_iqm import transpile_to_IQM

print(f"qaas {version('qaas')} from {qaas.__file__}")
print(f"project={lexis_project} resource={resource_name} shots={shots}")

# Connect
provider = QProvider(lexis_project, token)
backend: QBackend = provider.get_backend(resource_name)
print(f"Qubits: {backend.architecture.qubits}")
print(f"Gates: {list(backend.architecture.gates.keys())}")

# Circuit
qc = QuantumCircuit(15, 5)
for i in range(15):
    qc.h(i)
for i in range(14):
    qc.cx(i, i + 1)
qc.cx(0, 14)
qc.measure(range(5), range(5))

qc_transpiled = transpile_to_IQM(qc, backend, optimize_single_qubits=False)


def check_counts(name: str, counts: dict) -> None:
    total = sum(counts.values())
    print(f"[{name}] counts: {counts}")
    assert counts, f"[{name}] empty counts"
    assert total == shots, f"[{name}] expected {shots} shots, got {total}"


# 1) Qiskit circuit submission
t = time()
job: QJob = backend.run([qc_transpiled], shots=shots)
result = job.result()
print(f"job_id={job.job_id} client time {time() - t:.1f}s")
check_counts("qiskit", result.get_counts())
print(f"Real HW execution {job.remote_hw_runtime} s")
print(f"QProvider runtime {job.qaas_runtime} s")
print(f"Allocation: {job.allocation_amount}, consumption: {job.consumpted_resources}")

# 2) OpenQASM 3 submission
qasm = qasm3dumps(qc_transpiled)
t = time()
qasm_job: QJob = backend.run([qasm], shots=shots)
qasm_result = qasm_job.result()
print(f"qasm job_id={qasm_job.job_id} client time {time() - t:.1f}s")
check_counts("qasm3", qasm_result.get_counts())

print("SMOKE TEST PASSED")
