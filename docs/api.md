---
icon: lucide/book-open
---

# API Reference

## QProvider

Entry point for quantum resource access through the LEXIS platform.

```python
from qaas import QProvider

provider = QProvider(token, project_name)
```

**Parameters**

| Name | Type | Description |
|------|------|-------------|
| `token` | `str` | LEXIS access token |
| `project_name` | `str` | LEXIS project identifier |

### `get_backend(resource_name)`

Returns a [`QBackend`](#qbackend) connected to the specified resource.

| Parameter | Type | Description |
|-----------|------|-------------|
| `resource_name` | `str` | Accounting string, e.g. `"EQE1-CZ-P0001"` |

---

## QBackend

Represents a quantum hardware interface. Obtained via `QProvider.get_backend()`.

### `architecture`

Hardware specifications object.

| Attribute | Description |
|-----------|-------------|
| `architecture.qubits` | List of available qubit indices |
| `architecture.gates` | Dict of supported gate names → definitions |

### `transpile(quantum_circuit, optimize_single_qubits=False)`

Compiles a Qiskit `QuantumCircuit` into the IQM native gate set.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `quantum_circuit` | `QuantumCircuit` | — | Qiskit circuit to compile |
| `optimize_single_qubits` | `bool` | `False` | Merge single-qubit gates where possible |

Returns a transpiled `QuantumCircuit` ready for execution.

### `run(quantum_circuit, shots=1000)`

Submits a transpiled circuit for execution.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `quantum_circuit` | `QuantumCircuit` | — | Transpiled circuit |
| `shots` | `int` | `1000` | Number of measurement repetitions |

Returns a [`QJob`](#qjob) handle.

---

## QJob

Manages an in-flight or completed quantum job. Obtained via `QBackend.run()`.

### `result()`

Blocks until the job is complete, then returns a result object.

```python
result = job.result()
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
