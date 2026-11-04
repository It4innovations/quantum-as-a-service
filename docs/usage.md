---
icon: lucide/play
---

# Usage

You need a LEXIS Platform project with an allocated quantum resource (for example the VLQ machine at IT4Innovations, VSB – Technical University of Ostrava). How to get an allocation for VLQ is described in the [IT4I documentation](https://docs.it4i.cz/en/docs/clusters/vlq/access); access to LEXIS projects is described there and in the [LEXIS Platform documentation](https://docs.lexis.tech/).

You will need two names from your project:

- **LEXIS project** — the project short name, e.g. `EU-XX-XXX`
- **Quantum resource** — the name of the quantum resource in that project, e.g. `VLQ-EU`

Ask the PI of your LEXIS project if you don't know them.

## Authentication

`QProvider` needs a LEXIS access token. There are two ways to provide it:

```python
from qheappe_client import QProvider

# 1. Interactive: without a token, a LEXIS login opens in the browser
provider = QProvider(lexis_project="YOUR_LEXIS_PROJECT")

# 2. Token: e.g. for scripts on remote machines or CI
import os
provider = QProvider(lexis_project="YOUR_LEXIS_PROJECT", token=os.environ["LEXIS_TOKEN"])
```

You can get a token once through an interactive `py4lexis` login:

```python
from py4lexis.session import LexisSession

token = LexisSession().get_access_token()
```

The client verifies the token and checks that you have access to the project and resource. If not, it raises `QAuthException` with the projects or resources you do have access to.

## Finding backends

A quantum resource can have one or more backends (quantum machines behind HEAppE clusters):

```python
backends = provider.available_backends("YOUR_QUANTUM_RESOURCE")
for name, md in backends.items():
    print(name, "available" if md.available else f"unavailable: {md.error}")
```

Detailed information about one backend:

```python
info = provider.get_backend_info("YOUR_QUANTUM_RESOURCE", backend_name="VLQ")
print(info.supplier, info.quantum_technology)
print("QScheduler:", info.qaas_version, "| station-control:", info.machine_info.station_control_version)
```

## Connecting to a backend

```python
backend = provider.get_backend(lexis_resource="YOUR_QUANTUM_RESOURCE")
```

If the resource has more than one backend, choose one with `backend_name="..."`.

Useful options of `get_backend()`:

| Option | Description |
|--------|-------------|
| `backend_name` | Backend (cluster) to use; required if the resource has several |
| `calibration_set_id` | Use a specific calibration set (UUID) instead of the current default |
| `use_metrics` | Load calibration quality metrics into the transpiler target (better qubit selection) |
| `strict_iqm_version` | `True` (default): raise if the installed IQM libraries don't work with the backend; `False`: only warn |

## Inspecting the hardware

```python
print("Qubits:", backend.architecture.qubits)
print("Gates:", list(backend.architecture.gates))
print("Calibration set:", backend.architecture.calibration_set_id)
print("Number of qubits:", backend.num_qubits)
```

## Transpiling circuits

Circuits must be transpiled to the IQM native gate set and qubit connectivity before they are run:

```python
from qiskit import QuantumCircuit

qc = QuantumCircuit(2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

qc_transpiled = backend.transpile(qc)
```

`transpile()` also accepts a list of circuits. Keyword arguments are passed to IQM's `transpile_to_IQM`, for example `optimize_single_qubits=False`, `initial_layout=...` or `restrict_to_qubits=[...]`.

## Running a job

```python
job = backend.run(qc_transpiled, shots=1000, job_name="bell_state")
print("HEAppE job ID:", job.job_id)
```

- `shots` is required.
- Pass a list of transpiled circuits to run several circuits in one job.
- `job_name` sets the name of the job in HEAppE.

## Reading results

`job.result()` waits until the job finishes. It returns a Qiskit `PrimitiveResult` by default, or a legacy Qiskit `Result` with `qiskit_legacy=True`:

```python
# PrimitiveResult: one entry per circuit
result = job.result(with_measurements=True)
meas = result[0].data.meas
print(meas.get_counts())
print(meas.get_bitstrings()[:10])  # per-shot results

# Legacy Result
legacy = job.result(qiskit_legacy=True, with_measurements=True)
print(legacy.get_counts())
print(legacy.get_memory()[:10])  # per-shot results
```

### PrimitiveResult vs. legacy Result

Both contain the same measurement data; they differ in shape and in which Qiskit code expects them.

| | `PrimitiveResult` (default) | Legacy `Result` (`qiskit_legacy=True`) |
|---|---|---|
| Qiskit API | Current Qiskit primitives format (as returned by `SamplerV2`) | Older `backend.run()` format (`qiskit.result.Result`) |
| Several circuits | One entry per circuit: `result[i]` | One experiment per circuit: `result.get_counts(i)` |
| Counts | `result[i].data.meas.get_counts()` | `result.get_counts()` |
| Per-shot results | `result[i].data.meas.get_bitstrings()` | `result.get_memory()` |
| Use it for | New code, Qiskit primitives-based workflows | Existing code and tutorials that call `get_counts()` on the result |

The measurements are always in the `meas` register of a `PrimitiveResult`.

| Argument | Default | Description |
|----------|---------|-------------|
| `with_measurements` | `False` | Include per-shot results (bitstrings / memory) |
| `key_order_left_to_right` | `False` | Bit order of the bitstrings; `False` matches Qiskit's right-to-left order |
| `qiskit_legacy` | `False` | Return a legacy `Result` instead of a `PrimitiveResult` |

## Getting the results of an earlier job

You can attach to a job submitted earlier using its HEAppE job ID:

```python
from qheappe_client import QJob

job = QJob(backend, 12345)
counts = job.result(qiskit_legacy=True).get_counts()
```

## Full example

```python
from qheappe_client import QProvider
from qiskit import QuantumCircuit

provider = QProvider(lexis_project="YOUR_LEXIS_PROJECT")
backend = provider.get_backend(lexis_resource="YOUR_QUANTUM_RESOURCE")

qc = QuantumCircuit(2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job = backend.run(backend.transpile(qc), shots=1000, job_name="bell_state")
counts = job.result(qiskit_legacy=True).get_counts()

for state, count in sorted(counts.items()):
    print(f"|{state}⟩: {count}")
```

## Error handling

All client errors derive from `QException`:

```python
from qheappe_client.utils import QAuthException, QException

try:
    backend = provider.get_backend(lexis_resource="YOUR_QUANTUM_RESOURCE")
    result = backend.run(backend.transpile(qc), shots=1000).result()
except QAuthException as e:
    print("Authentication / access problem:", e)
except QException as e:
    print("QHEAppE Client error:", e)
```

Common causes:

- Expired or invalid LEXIS token
- Wrong LEXIS project or quantum resource name (the message lists the available ones)
- Backend unavailable or under maintenance
- Installed IQM libraries don't work with the backend (the message gives the `pip install` command)
- Circuits not transpiled for the backend

## Async API

`QProvider` and the backends use a synchronous API that also works inside Jupyter. For async code, `QClient` offers the same operations as coroutines:

```python
import asyncio
from qheappe_client.qclient import QClient

async def main():
    async with await QClient.create(TOKEN, "YOUR_LEXIS_PROJECT", "YOUR_QUANTUM_RESOURCE") as client:
        md = await client.get_backend()
        print(md.backend_name, md.available)
        metrics = await client.machine_metrics()

asyncio.run(main())
```
