---
icon: lucide/book-open
---

# API Reference

## QProvider

Entry point for quantum resource access through the LEXIS Platform.

```python
from qaas.client import QProvider

provider: QProvider = QProvider(lexis_project, token)
```

**Parameters**

| Name | Type | Description |
|------|------|-------------|
| `lexis_project` | `str` | LEXIS Platform project identifier |
| `token` | `str` | User's LEXIS Platform access token |


### `get_backend(resource_name) -> QBackendIQM`

Returns a [`QBackendIQM`](#qbackend) connected to the specified resource.

| Parameter | Type | Description |
|-----------|------|-------------|
| `lexis_resource` | `str` | Accounting string, e.g. `"EU-XX-XXXX"` |

---

## QBackendIQM

Represents a quantum hardware interface. Obtained via `QProvider.get_backend(lexis_resource)`.

### `architecture`

Hardware specifications object.

| Attribute | Description |
|-----------|-------------|
| `architecture.qubits` | List of available qubit indices |
| `architecture.gates` | Dict of supported gate names → definitions |

### `transpile(quantum_circuit) -> QuantumCircuit`

Transpiles a Qiskit `QuantumCircuit` into the IQM native gate set.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `quantum_circuit` | `QuantumCircuit` | — | Qiskit circuit to transpile |
| `optimize_single_qubits` | `bool` | `True` | Whether to optimize single qubit gates away |

Returns a transpiled `QuantumCircuit` ready for execution.

### `run(quantum_circuit, shots=1000) -> QJob`

Submits a transpiled circuit for execution.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `quantum_circuit` | `QuantumCircuit` | — | Transpiled quantum circuit |
| `shots` | `int` | `1000` | Number of measurement repetitions |

Returns a [`QJob`](#qjob) handle.

---

## QJob

Manages an in-flight or completed quantum job. Obtained via `QBackend.run()`.

### `result() -> Result`

Blocks until the job is complete, then returns a `Result` object.

```python
result: Result = job.result()
```

### Result object

| Method | Returns | Description |
|--------|---------|-------------|
| `get_counts()` | `dict[str, int]` | Bitstring → measurement count mapping |

---

## QException

Base exception class raised by QaaS for all error conditions.

```python
from qaas import QException

try:
    ...
except QException as e:
    print(e)
```
