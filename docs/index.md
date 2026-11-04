---
icon: lucide/atom
---

# QHEAppE Client

**QHEAppE Client** is the Python client of **QaaS (Quantum-as-a-Service)**, developed by IT4Innovations. It gives access to IQM quantum computers, such as the VLQ machine at IT4Innovations, through the [LEXIS Platform](https://docs.lexis.tech/) and [HEAppE](https://heappe.eu/).

You write circuits in [Qiskit](https://www.ibm.com/quantum/qiskit); the client handles LEXIS authentication, finds the quantum backends of your LEXIS project, transpiles circuits for the hardware, submits jobs through the HEAppE **QScheduler** API and returns the results as Qiskit objects.

The client is distributed in the `qaas` package and imported as `qheappe_client`.

## Quick start

Install the client with the IQM libraries (Python 3.12):

```bash
pip install 'qaas[iqm_qiskit]' --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple
```

Run a Bell-state circuit:

```python
from qheappe_client import QProvider
from qiskit import QuantumCircuit

# Without a token, a browser login to LEXIS opens
provider = QProvider(lexis_project="YOUR_LEXIS_PROJECT")

backend = provider.get_backend(lexis_resource="YOUR_QUANTUM_RESOURCE")

qc = QuantumCircuit(2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job = backend.run(backend.transpile(qc), shots=1000)
counts = job.result(qiskit_legacy=True).get_counts()
print(counts)  # e.g. {'00': 497, '11': 489, '01': 8, '10': 6}
```

`job.result()` returns a Qiskit `PrimitiveResult` by default, or a legacy `Result` with `qiskit_legacy=True`. See [PrimitiveResult vs. legacy Result](usage.md#primitiveresult-vs-legacy-result) for the difference.

## Pages

| Page | Description |
|------|-------------|
| [Installation](installation.md) | Requirements, IQM extras, backend compatibility |
| [Usage](usage.md) | Authentication, backends, running circuits, reading results |
| [API Reference](api.md) | `QProvider`, `QBackendIQM`, `QJob`, `QBackendMetadata`, exceptions |
| [Architecture](architecture.md) | Components and the job flow through LEXIS and HEAppE QScheduler |

## Authors

- Jan Swiatkowski, jan.swiatkowski@vsb.cz
- Ladislav Foltyn, ladislav.foltyn@vsb.cz
- Ada Böhm, ada.bohm@vsb.cz
- Jakub Konvička, jakub.konvicka@vsb.cz

## License

Apache 2.0 — see the `LICENSE` file.
