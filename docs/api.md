---
icon: lucide/book-open
---

# API Reference

This page covers the main classes of the QHEAppE Client. The full, generated reference of every module is in the [API reference (pdoc)](../api_reference/qheappe_client.html).

```python
from qheappe_client import QProvider, QBackendIQM, QJob, QBackendMetadata
from qheappe_client.utils import QException, QAuthException
```

---

## QProvider

Entry point. Authenticates to LEXIS and creates backends for the quantum resources of a LEXIS project. Clients are cached per resource.

```python
provider = QProvider(lexis_project, token=None, lexis_api_url="https://api.lexis.tech/")
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `lexis_project` | `str` | LEXIS project short name |
| `token` | `str \| None` | LEXIS access token; if `None`, an interactive `py4lexis` login is started |
| `lexis_api_url` | `str` | LEXIS API base URL |

### `get_backend(lexis_resource, backend_name=None, calibration_set_id=None, *, use_metrics=False, strict_iqm_version=True) -> QBackendIQM`

Creates a backend for a quantum resource. Checks that the installed IQM libraries work with the backend's station-control version.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lexis_resource` | `str \| QBackendMetadata` | — | Quantum resource name, or metadata from `available_backends()` |
| `backend_name` | `str \| None` | `None` | Backend (cluster) name; required if the resource has several |
| `calibration_set_id` | `UUID \| None` | `None` | Calibration set; `None` = current default |
| `use_metrics` | `bool` | `False` | Load calibration metrics into the transpiler target |
| `strict_iqm_version` | `bool` | `True` | Raise `QException` on an IQM version mismatch; `False` only warns |

### `available_backends(lexis_resource) -> dict[str, QBackendMetadata]`

All backends of a resource, including unavailable ones (check `.available` and `.error`).

### `get_backend_info(lexis_resource, backend_name=None, calibration_set_id=None) -> QBackendMetadata`

Metadata of one backend, including its architecture, without creating a backend.

### `get_client(lexis_resource, backend_name=None) -> QClient`

Low-level client bound to a resource or backend (machine metrics, job info, sessions).

### `get_pulla(...)`

Not available with `iqm_qiskit` (iqm-client 36): raises `NotImplementedError`. Requires `qaas[iqm35_qiskit]`.

### `clear_cache()`

Forget the cached clients; the next call connects again.

---

## QBackendIQM

Qiskit `BackendV2` for an IQM quantum computer. Returned by `QProvider.get_backend()`.

### `architecture`

The backend's dynamic quantum architecture (from the active calibration set).

| Attribute | Description |
|-----------|-------------|
| `architecture.qubits` | Qubit names |
| `architecture.gates` | Supported gates and their loci |
| `architecture.calibration_set_id` | Calibration set the architecture belongs to |

`num_qubits` and `target` are the standard Qiskit backend properties.

### `transpile(circuits, **kwargs) -> QuantumCircuit | list[QuantumCircuit]`

Transpiles one circuit or a list of circuits to the native gate set and connectivity. Keyword arguments go to IQM's `transpile_to_IQM` (e.g. `optimize_single_qubits`, `initial_layout`, `restrict_to_qubits`).

### `run(run_input, shots, **options) -> QJob`

Submits one transpiled circuit or a list of them as one job.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_input` | `QuantumCircuit \| list[QuantumCircuit]` | — | Transpiled circuit(s) |
| `shots` | `int` | — | Number of shots (required) |
| `job_name` | `str` | `"qiskit_qasm_job"` | Job name in HEAppE |
| `walltime_limit_secs` | `int` | `3600` | Walltime limit of the job |
| `circuit_compilation_options` | `CircuitCompilationOptions` | IQM default | IQM server-side compilation options |

### `create_circuit_job_definition(run_input, shots=1024, ...) -> CircuitJobDefinition`

Builds the IQM job definition without submitting it. `create_run_request` is an alias (iqm-client 35 name).

### `iqm_stack`

Which IQM library generation the backend uses (`IQMStack.IQM36` or `IQMStack.IQM35`).

---

## QJob

A submitted job. Returned by `QBackendIQM.run()`, or created from an existing HEAppE job ID with `QJob(backend, job_id)`.

### `job_id`

HEAppE job ID as an `int`. `job.job_id()` returns it as a string (Qiskit style).

### `result(with_measurements=False, key_order_left_to_right=False, qiskit_legacy=False) -> PrimitiveResult | Result`

Waits until the job finishes and returns the results.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `with_measurements` | `False` | Include per-shot results |
| `key_order_left_to_right` | `False` | Bit order of bitstrings; `False` = Qiskit's right-to-left order |
| `qiskit_legacy` | `False` | Return a legacy `Result` instead of a `PrimitiveResult` |

Accessing the results:

| Result type | Counts | Per-shot results |
|-------------|--------|------------------|
| `PrimitiveResult` | `res[i].data.meas.get_counts()` | `res[i].data.meas.get_bitstrings()` |
| `Result` (legacy) | `res.get_counts()` | `res.get_memory()` |

Raises `RuntimeError` if the job failed or was cancelled.

### `status() -> JobStatus`

Current job state as a Qiskit `JobStatus`.

---

## QBackendMetadata

Describes one backend of a quantum resource (returned by `available_backends()` and `get_backend_info()`).

| Property | Description |
|----------|-------------|
| `backend_name` | Backend (cluster) name |
| `available` / `error` | Whether the backend can be used, and why not |
| `supplier`, `quantum_technology` | e.g. `IQM`, `SUPERCONDUCTING` |
| `qaas_version` | QScheduler version |
| `machine_info` | Parsed `MachineInfo`, incl. `station_control_version` |
| `dynamic_architecture` / `static_architecture` | IQM architectures, if loaded |
| `lexis_project`, `lexis_resource`, `heappe_url`, `allocated_units` | LEXIS and HEAppE details |

---

## Exceptions

| Exception | Raised when |
|-----------|-------------|
| `QException` | Base class of all client errors |
| `QAuthException` | Token is invalid or you have no access to the project/resource |
| `QResultsFailed` | Results of a job could not be retrieved |

```python
from qheappe_client.utils import QException

try:
    ...
except QException as e:
    print(e)
```
