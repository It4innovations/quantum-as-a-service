"""Smoke test of a qaas installation (qheappe_client, qaas >= 1.1).

Env vars (from .env_install_test; the names of the live test suite also work):
  ACCESS_TOKEN   (or LEXIS_TOKEN)       LEXIS access token
  LEXIS_PROJECT  (or QHEAPPE_PROJECT)   LEXIS project short name
  RESOURCE_NAME  (or QHEAPPE_RESOURCE)  LEXIS quantum resource
  BACKEND_NAME   (or QHEAPPE_BACKEND)   optional, backend (cluster) name, e.g. VLQ
  LEXIS_API_URL  (or QHEAPPE_LEXIS_API_URL) optional, LEXIS API base URL
  SHOTS          optional, default 100
  SKIP_QPU       optional, 1 = install, import and connect only; submit no jobs
"""

import os
import sys
from importlib.metadata import version
from time import time

os.environ.setdefault("QPROVIDER_LOGLEVEL", "INFO")


def env(*names: str, required: bool = True) -> str | None:
    for name in names:
        if value := os.environ.get(name) or os.environ.get(name.lower()):
            return value
    if required:
        sys.exit(f"Missing env var {' or '.join(names)}")
    return None


token = env("ACCESS_TOKEN", "LEXIS_TOKEN")
lexis_project = env("LEXIS_PROJECT", "QHEAPPE_PROJECT")
resource_name = env("RESOURCE_NAME", "QHEAPPE_RESOURCE")
backend_name = env("BACKEND_NAME", "QHEAPPE_BACKEND", required=False)
lexis_api_url = env("LEXIS_API_URL", "QHEAPPE_LEXIS_API_URL", required=False)
shots = int(os.environ.get("SHOTS", "100"))
skip_qpu = os.environ.get("SKIP_QPU", "") not in ("", "0", "false", "False")

# Imports that need the iqm_qiskit extra: a missing extra fails here, before any job.
from qiskit import QuantumCircuit

import qheappe_client
from qheappe_client import QProvider, QPulla, QSamplerV2

print(f"qaas {version('qaas')} from {qheappe_client.__file__}")
for pkg in ("qiskit", "iqm-client", "iqm-pulla", "iqm-exa-common", "py4lexis"):
    print(f"  {pkg} {version(pkg)}")
print(f"project={lexis_project} resource={resource_name} backend={backend_name}")

# Connect (no allocation used)
kwargs = {"lexis_api_url": lexis_api_url} if lexis_api_url else {}
provider = QProvider(lexis_project=lexis_project, token=token, **kwargs)
backend = provider.get_backend(lexis_resource=resource_name, backend_name=backend_name)
print(f"Backend: {backend.name}, stack {backend.iqm_stack}")
print(f"Qubits: {backend.architecture.qubits}")
assert backend.architecture.qubits, "backend reports no qubits"

pulla = provider.get_pulla(lexis_resource=resource_name, backend_name=backend_name)
assert isinstance(pulla, QPulla)
print(f"QPulla: chip {pulla.get_chip_label()}")

if skip_qpu:
    print("SKIP_QPU set: no jobs submitted")
    print("SMOKE TEST PASSED")
    sys.exit(0)

qc = QuantumCircuit(2, name="bell")
qc.h(0)
qc.cx(0, 1)
qc.measure_all()
qc_transpiled = backend.transpile(qc)


def check_counts(name: str, counts: dict) -> None:
    total = sum(counts.values())
    print(f"[{name}] counts: {counts}")
    assert counts, f"[{name}] empty counts"
    assert total == shots, f"[{name}] expected {shots} shots, got {total}"
    assert all(len(k) == 2 for k in counts), f"[{name}] unexpected bitstrings"


# 1) backend.run() -> qiskit Result
t = time()
job = backend.run(qc_transpiled, shots=shots)
result = job.result()
print(f"[run] job_id={job.job_id()} status={job.status().name} {time() - t:.1f}s")
check_counts("run", result.get_counts(qc_transpiled))

# 2) QSamplerV2 -> PrimitiveResult
t = time()
sampler_job = QSamplerV2(backend).run([qc_transpiled], shots=shots)
pub = sampler_job.result()[0]
print(f"[sampler] job_id={sampler_job.job_id()} {time() - t:.1f}s")
check_counts("sampler", pub.data.meas.get_counts())

print("SMOKE TEST PASSED")
