---
icon: lucide/play
---

# Usage

User needs to have a valid LEXIS Platform project with allocated VLQ resources (quantum resources in general). How to get allocation to use the VLQ quantum machine located at IT4Innovations, VSB -- Technical university of Ostrava, follow the instructions in [IT4I official documentation](https://docs.it4i.cz/en/docs/clusters/vlq/access). How to request the access to the LEXIS Platform project is described in the IT4I documentation as well, or one can use the [LEXIS Platform documentation](https://docs.lexis.tech/).

## Authentication

QaaS requires a valid LEXIS Platform access token. The recommended approach is automatic retrieval via `py4lexis` which handles user's authentication

```python
from py4lexis.session import LexisSession

token: str = LexisSession().get_access_token()
```

## Connecting to a backend

```python
from qaas.client import QProvider
from qaas.client.backend_iqm import QBackendIQM

provider: QProvider = QProvider(
    lexis_project="LEXIS_PLATFORM_PROJECT_IDENTIFIER", token="USER_ACCESS_TOKEN"
)
backend: QBackendIQM = provider.get_backend(lexis_resource="QUANTUM_RESOURCE")
```

The `lexis_resource` string (e.g. `"EU-XX-XXXX"`) maps to a specific quantum backend. Contact your LEXIS Platform project manager (PI of the LEXIS Platform project) for the resource name available to your project.

## Inspecting hardware

```python
print("Qubits:", backend.architecture.qubits)
print("Gates: ", list(backend.architecture.gates.keys()))
```

## Circuit transpilation

Qiskit circuits must be transpiled for the IQM gate set before submission:

```python
from qiskit import QuantumCircuit

qc: QuantumCircuit = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

qc_transpiled: QuantumCircuit = backend.transpile(qc, optimize_single_qubits=False)
```

`optimize_single_qubits` (default `False`) enables single-qubit gate merging. Leave it disabled unless you have verified that the optimisation is safe for your circuit.

## Running a job

```python
from qaas.client.backend_iqm import QJob

job: QJob = backend.run(qc_transpiled, shots=1000)
```

## Retrieving results

```python
from qiskit.result import Result

result: Result = job.result()
counts: dict[str, int] | list[dict[str, int]] = result.get_counts()

for bitstring, count in counts.items():
    if count > 0:
        print(f"|{bitstring}⟩: {count}")
```

## Full example

```python
from py4lexis.session import LexisSession
from qaas.client import QProvider
from qaas.client.backend_iqm import QBackendIQM, QJob
from qiskit import QuantumCircuit
from qiskit.result import Result

token: str = LexisSession().get_access_token()
provider: QProvider = QProvider(token, "LEXIS_PLATFORM_PROJECT_IDENTIFIER")
backend: QBackendIQM = provider.get_backend("QUANTUM_RESOURCE")

qc: QuantumCircuit = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

qc_transpiled: QuantumCircuit = backend.transpile(qc, optimize_single_qubits=False)
job: QJob = backend.run(qc_transpiled, shots=1000)
result: Result = job.result()
counts: dict[str, int] | list[dict[str, int]] = result.get_counts()

for state, count in counts.items():
    if count > 0:
        print(f"State '{state}': {count} counts")
```

## Error handling

All QaaS errors are raised as `QException`:

```python
from qaas.client import QProvider
from qaas.client.backend_iqm import QBackendIQM
from qaas.client.utils import QException
from qiskit.result import Result

try:
    provider: QProvider = QProvider(project, token)
    backend: QBackendIQM = provider.get_backend(resource)
    result: Result = backend.run(circuit, shots=1000).result()
except QException as e:
    print(f"QaaS error: {e}")
```

Common failure modes:

- LEXIS Platform authentication failures (expired/invalid token)
- Invalid LEXIS Platform project or quantum resource name
- Backend connectivity issues
- Circuit transpilation errors (unsupported gates)
- Job execution failures (hardware unavailable)