---
icon: lucide/play
---

# Usage

## Authentication

QaaS requires a valid LEXIS access token. The recommended approach is automatic retrieval via `py4lexis`:

=== "Automatic (recommended)"

    ```python
    from py4lexis.session import LexisSession

    token = LexisSession().get_access_token()
    ```

=== "Manual"

    ```python
    token = "your_lexis_access_token"
    ```

## Connecting to a backend

```python
from qaas import QProvider

provider = QProvider(token, project_name="my_lexis_project")
backend  = provider.get_backend("EQE1-CZ-P0001")
```

The `resource_name` string (e.g. `"EQE1-CZ-P0001"`) maps to a specific quantum backend. Contact your LEXIS project manager for the resource name available to your project.

## Inspecting hardware

```python
print("Qubits:", backend.architecture.qubits)
print("Gates: ", list(backend.architecture.gates.keys()))
```

## Circuit transpilation

Qiskit circuits must be transpiled for the IQM gate set before submission:

```python
from qiskit import QuantumCircuit

qc = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

qc_transpiled = backend.transpile(qc, optimize_single_qubits=False)
```

`optimize_single_qubits` (default `False`) enables single-qubit gate merging. Leave it disabled unless you have verified that the optimisation is safe for your circuit.

## Running a job

```python
job = backend.run(qc_transpiled, shots=1000)
```

## Retrieving results

```python
result = job.result()
counts = result.get_counts()

for bitstring, count in counts.items():
    if count > 0:
        print(f"|{bitstring}⟩: {count}")
```

## Full example

```python
from py4lexis.session import LexisSession
from qaas import QProvider
from qiskit import QuantumCircuit

token    = LexisSession().get_access_token()
provider = QProvider(token, "vlq_demo_project")
backend  = provider.get_backend("qaas_user")

qc = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job    = backend.run(backend.transpile(qc, optimize_single_qubits=False), shots=1000)
counts = job.result().get_counts()

for state, count in counts.items():
    if count > 0:
        print(f"State '{state}': {count} counts")
```

## Error handling

All QaaS errors are raised as `QException`:

```python
from qaas import QProvider, QException

try:
    provider = QProvider(token, project)
    backend  = provider.get_backend(resource)
    result   = backend.run(circuit, shots=1000).result()
except QException as e:
    print(f"QaaS error: {e}")
```

Common failure modes:

- LEXIS authentication failures (expired/invalid token)
- Invalid project or resource name
- Backend connectivity issues
- Circuit transpilation errors (unsupported gates)
- Job execution failures (hardware unavailable)
