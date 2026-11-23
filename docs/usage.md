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

provider = QProvider(
    lexis_project="YOUR_LEXIS_PROJECT", token=os.environ["LEXIS_TOKEN"]
)
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
print(
    "QScheduler:",
    info.qaas_version,
    "| station-control:",
    info.machine_info.station_control_version,
)
```

## Connecting to a backend

```python
backend = provider.get_backend(
    lexis_resource="YOUR_QUANTUM_RESOURCE", backend_name="VLQ"
)
```

`backend_name` selects the backend (here the VLQ machine). You can leave it out if the resource has only one backend.

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

- `shots` defaults to `backend.options.shots` (1024).
- Pass a list of transpiled circuits to run several circuits in one job.
- `job_name` sets the name of the job in HEAppE.

### Waiting for a job

To wait until a job finishes without reading its results yet, use `job.wait_for_final_state()` instead of calling `job.status()` in a loop:

```python
import time

from qiskit.providers.jobstatus import JobStatus

# Recommended: returns as soon as HEAppE reports the job finished
job.wait_for_final_state(timeout=600)

# Avoid: one HEAppE request per loop, and up to 10 s late
while job.status() not in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
    time.sleep(10)
```

`wait_for_final_state()` listens to HEAppE's event stream (a websocket), so:

- It returns as soon as the job finishes, not at the next poll.
- A `job.status()` loop reads HEAppE's job information, which can still say the job is queued for a few seconds after it finished. The event stream reports the finish first.
- It needs fewer requests for the same response time. Polling only notices the finish faster if you poll more often.

The event stream only reports changes that happen after it connects. So `wait_for_final_state()` also asks HEAppE for the job's state every `wait` seconds (default 5) as a backup. You can raise `wait` (e.g. `wait=60`) to send fewer requests: the event stream still ends the wait as soon as the job finishes.

| Argument | Default | Description |
|----------|---------|-------------|
| `timeout` | `None` | Seconds to wait before raising `JobTimeoutError`; `None` waits indefinitely |
| `wait` | `5` | Seconds between backup status requests |
| `callback` | `None` | Called as `callback(job_id, status, job)` with the current status and on each status change before the final one |

To follow progress:

```python
job.wait_for_final_state(
    callback=lambda job_id, status, job: print(job_id, status.name)
)
```

`job.result()` waits the same way, so you don't need `wait_for_final_state()` before `result()`. Sampler jobs from `QSamplerV2` have the same method.

## Reading results

`job.result()` waits until the job finishes and returns a Qiskit `Result`, as `backend.run()` does in Qiskit:

```python
job = backend.run(qc_transpiled, shots=1000, memory=True)
result = job.result()
print(result.get_counts())
print(result.get_memory()[:10])  # per-shot results (needs memory=True)
```

With several circuits in one job, use `result.get_counts(i)` and `result.get_memory(i)`.

Bitstrings follow the Qiskit order: classical registers in circuit order, bit 0 rightmost. With several classical registers, they are separated by a space (e.g. `"0 01"`), as in Qiskit.

Each result is named after its circuit and carries the circuit's metadata, so `result.get_counts(circuit)` works. A job re-created from its ID (below) has no access to the circuits: its results are named `circuit-0`, `circuit-1`, … with empty metadata, but keep the classical registers.

Because `job.result()` returns a `Result`, Qiskit tools that take a backend work unchanged, e.g. `qiskit.primitives.BackendSamplerV2(backend=backend)`, `BackendEstimatorV2` and `qiskit-experiments`.

### Sampler primitive (PrimitiveResult)

For results in the Qiskit primitives format, use `QSamplerV2`. It takes sampler PUBs (circuits, optionally with parameter values and shots) and returns a `PrimitiveResult` with one entry per PUB and one `BitArray` per classical register:

```python
from qheappe_client import QSamplerV2

sampler = QSamplerV2(backend)
job = sampler.run([backend.transpile(qc)], shots=1000)
result = job.result()
meas = result[0].data.meas  # register name, "meas" for measure_all()
print(meas.get_counts())
print(meas.get_bitstrings()[:10])  # per-shot results
```

- Circuits must be transpiled first (`backend.transpile(qc)`).
- Parameterized circuits: `sampler.run([(circuit, parameter_values)])`. The result keeps the shape of `parameter_values`.
- PUBs with different shot counts are submitted as separate HEAppE jobs; `job.jobs` lists them.
- `QSamplerV2(backend, default_shots=..., run_options={...})` sets the default shots and extra `backend.run()` options such as `job_name`.

| | Legacy `Result` (`job.result()`) | `PrimitiveResult` (`QSamplerV2`) |
|---|---|---|
| Several circuits | `result.get_counts(i)` | `result[i]` |
| Counts | `result.get_counts()` | `result[i].data.<register>.get_counts()` |
| Per-shot results | `result.get_memory()` (with `memory=True`) | `result[i].data.<register>.get_bitstrings()` |
| Use it for | Existing code, tutorials, tools that take a backend | Qiskit primitives workflows, parameter sweeps |

## Getting the results of an earlier job

You can attach to a job submitted earlier using its HEAppE job ID:

```python
from qheappe_client import QJob

job = QJob(backend, 12345)
counts = job.result().get_counts()

# with per-shot results
memory = QJob(backend, 12345, memory=True).result().get_memory()
```

## Full example

```python
from qheappe_client import QProvider
from qiskit import QuantumCircuit

provider = QProvider(lexis_project="YOUR_LEXIS_PROJECT")
backend = provider.get_backend(
    lexis_resource="YOUR_QUANTUM_RESOURCE", backend_name="VLQ"
)

qc = QuantumCircuit(2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job = backend.run(backend.transpile(qc), shots=1000, job_name="bell_state")
counts = job.result().get_counts()

for state, count in sorted(counts.items()):
    print(f"|{state}⟩: {count}")
```

## Pulse-level access (Pulla)

`get_pulla()` gives an IQM pulse-level client (`QPulla`, an `iqm.iqm_client.IQMClient`) for a backend. Compile circuits on your machine with IQM's compiler, then run the compiled job through HEAppE:

```python
pulla = provider.get_pulla(lexis_resource="YOUR_QUANTUM_RESOURCE", backend_name="VLQ")

qc = QuantumCircuit(2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

# Qiskit circuit -> IQM circuit + compiler for this backend
circuits, compiler = pulla.qiskit_to_iqm(pulla.backend.transpile(qc))

settings = compiler.get_settings(circuits=circuits)
settings.set_shots(1000)
run_definition, context = compiler.compile(circuits, settings=settings)

job = pulla.submit_playlist(run_definition, context=context)
job.wait_for_completion()
print(pulla.sweep_job_to_qiskit(job, shots=1000).get_counts())
```

Between `get_settings()` and `compile()` you can change pulse-level settings, or build schedules yourself with `compiler.get_schedule_builder()`; see IQM's Pulla documentation.

Example: [`examples/qheappe_client/pulse_level_custom_x_gate.ipynb`](https://github.com/It4innovations/quantum-as-a-service/blob/main/examples/qheappe_client/pulse_level_custom_x_gate.ipynb) defines a custom pulse shape for the `prx` gate, calibrates its amplitude with Rabi sweeps (`Sweep` over gate parameters), and compares X and X·X with the native gate. It follows the calibration workflow of [Filippos Dakis, Experimental_results_IQM](https://github.com/Filippos-Dakis/Experimental_results_IQM).

Requirements:

- `qaas[iqm_qiskit]` (iqm-client 36.x, iqm-pulla 15.x)
- QScheduler v0.2.2 or newer, with pulse access enabled for the machine
- HEAppE 6.7.0 or newer (forwards the task `Kind`, machine artifacts and whole calibration sets)

## Error handling

All client errors derive from `QException`:

```python
from qheappe_client.utils import QAuthException, QException

try:
    backend = provider.get_backend(
        lexis_resource="YOUR_QUANTUM_RESOURCE", backend_name="VLQ"
    )
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
    async with await QClient.create(
        TOKEN, "YOUR_LEXIS_PROJECT", "YOUR_QUANTUM_RESOURCE"
    ) as client:
        md = await client.get_backend(backend_name="VLQ")
        print(md.backend_name, md.available)
        metrics = await client.machine_metrics()


asyncio.run(main())
```
