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

### `get_pulla(lexis_resource, backend_name=None, calibration_set_id=None, *, strict_iqm_version=True, walltime_limit_secs=3600, job_name="qpulla_job", use_sessions=False) -> QPulla`

Pulse-level access to a backend, see [QPulla](#qpulla). Needs `qaas[iqm_qiskit]` (iqm-client 36) and QScheduler v0.2.2+ with pulse access enabled for the machine; raises `NotImplementedError` with another IQM stack.

| Argument | Default | Description |
|----------|---------|-------------|
| `walltime_limit_secs` | `3600` | HEAppE walltime limit of each pulse task |
| `job_name` | `"qpulla_job"` | HEAppE job name |
| `use_sessions` | `False` | Run tasks in QScheduler sessions |

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

### `client -> QPulla`

IQM client of the backend, connected through HEAppE (created on first use, IQM 36 stack only). It lets IQM's helpers that take a backend work with this one, e.g. `iqm.qiskit_iqm.pulse_level_utils.qiskit_to_iqm(backend, circuits)`.

---

## QPulla

Pulse-level access (IQM Pulla) to one backend. A subclass of `iqm.iqm_client.IQMClient` whose requests go through HEAppE/QScheduler, so IQM's pulse-level API works as IQM documents it. Returned by `QProvider.get_pulla()`.

| Member | Description |
|--------|-------------|
| `backend` | The `QBackendIQM` of the same machine |
| `qiskit_to_iqm(circuits)` | Transpiled Qiskit circuits → IQM circuits and a compiler with the backend's qubit mapping |
| `get_qiskit_compiler(...)` | Compiler that also transpiles Qiskit circuits |
| `get_standard_compiler(...)` | IQM's standard compiler (default calibration set) |
| `submit_playlist(run_definition, context=...)` | Submit a compiled `RunDefinition`; returns IQM's `RunJobTracker` |
| `sweep_job_to_qiskit(job, shots=...)` | Results of a finished job as a Qiskit `Result` |
| `get_calibration_set()`, `get_calibration_set_stash()` | Calibration data |
| `get_chip_label()`, `get_chip_topology()` | Chip information |
| `get_dynamic_quantum_architecture()`, `get_static_quantum_architecture()` | Architectures |
| `executor` | Submission options: `walltime_limit_secs`, `job_name`, `use_sessions` |

The returned `RunJobTracker` works as in IQM's client: `wait_for_completion()`, `status`, `cancel()`, `result()`, `result_sweep()`. Its `job_id` is the HEAppE job id as a UUID (`UUID(int=heappe_job_id)`).

Writing calibration data (observations, runs, sequences) and `get_health()` are not available through HEAppE and raise `NotImplementedError`.

---

## QJob

A submitted job. Returned by `QBackendIQM.run()`, or created from an existing HEAppE job ID with `QJob(backend, job_id)`.

### `job_id`

HEAppE job ID as an `int`. `job.job_id()` returns it as a string (Qiskit style).

### `result() -> Result`

Waits until the job finishes and returns the results as a Qiskit `Result`. Per-shot results are included if the job was submitted with `backend.run(..., memory=True)`, or created with `QJob(backend, job_id, memory=True)`.

Accessing the results: `res.get_counts()`, `res.get_memory()` (per-shot results); `res.get_counts(i)` or `res.get_counts(circuit)` for one circuit. Results are named after their circuits and carry the circuit metadata; for a job created with `QJob(backend, job_id)`, they are named `circuit-<i>` with empty metadata. For a `PrimitiveResult`, use [`QSamplerV2`](#qsamplerv2).

Raises `RuntimeError` if the job failed or was cancelled.

### `status() -> JobStatus`

Current job state as a Qiskit `JobStatus`. Queries HEAppE on each call until the job reaches a final state.

### `wait_for_final_state(timeout=None, wait=5, callback=None)`

Waits until the job is `DONE`, `ERROR` or `CANCELLED`. Same interface as Qiskit's `JobV1.wait_for_final_state`, but follows HEAppE's event websocket instead of polling. The job info is still fetched every `wait` seconds as a backup, since the websocket only reports changes made after it connects. `callback(job_id, status, job)` is called with the current status and on each later status change before the final one. Raises `JobTimeoutError` after `timeout` seconds.

---

## QSamplerV2

Qiskit `SamplerV2` primitive (`BaseSamplerV2`) for a QaaS backend.

```python
from qheappe_client import QSamplerV2

sampler = QSamplerV2(backend, default_shots=None, run_options=None)
job = sampler.run(pubs, shots=None)
result = job.result()  # PrimitiveResult
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `backend` | | `QBackendIQM` to run on |
| `default_shots` | `None` | Shots for PUBs that set none; the backend's `shots` option if `None` |
| `run_options` | `None` | Extra `backend.run()` options, e.g. `job_name` |

`run(pubs, shots=None)` takes transpiled circuits or `(circuit, parameter_values[, shots])` tuples and returns a `QSamplerJob`. All PUBs with the same shot count go into one HEAppE job.

`QSamplerJob`:

| Member | Description |
|--------|-------------|
| `result()` | `PrimitiveResult`: per PUB, a `DataBin` with one `BitArray` per classical register, shaped like the PUB |
| `status()`, `done()`, `running()`, `cancelled()`, `in_final_state()` | State of the HEAppE jobs (the least advanced one wins) |
| `wait_for_final_state(timeout=None, wait=5, callback=None)` | Wait for all HEAppE jobs (see `QJob.wait_for_final_state`); `timeout` is shared, `callback` gets this job's combined status |
| `cancel()` | Cancel all HEAppE jobs |
| `jobs` | The underlying `QJob`s |
| `job_id()` | HEAppE job IDs, comma-separated |

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
