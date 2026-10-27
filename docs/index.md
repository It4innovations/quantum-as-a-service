---
icon: lucide/atom
---

# QaaS — Quantum-as-a-Service

**QaaS** provides access to quantum computing hardware through the [LEXIS platform](https://docs.lexis.tech/) via HEAppE. It targets IQM quantum hardware and aims to offer a vendor-neutral interface for academic quantum infrastructure.

## Quick start

QaaS requires Python 3.12 to be installed, as well as the `py4lexis` package which handles authentication of a user
```bash
pip install --index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple py4lexis
```

Then, feel free to install the QaaS package using PyPI repository

```bash
pip install qaas
```

Minimal example of quantum circuit submission to VLQ machine can looks like the following one

```python
from py4lexis.session import LexisSession
from qaas.client import QProvider
from qaas.client.backend_iqm import QBackendIQM
from qiskit import QuantumCircuit

# Authenticate to the LEXIS Platform and get access token
token: str = LexisSession().get_access_token()

# Connect to hardware
provider = QProvider(lexis_project="LEXIS_PLATFORM_PROJECT_IDENTIFIER",
                     token=token)

backend: QBackendIQM = provider.get_backend(lexis_resource="QUANTUM_RESOURCE")

# Run a Bell-state circuit
qc: QuantumCircuit = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job = backend.run(backend.transpile(qc), shots=1000)
counts = job.result().get_counts()
print(counts)
```

## Pages

| Page | Description |
|------|-------------|
| [Installation](installation.md) | Prerequisites and install steps |
| [Usage](usage.md) | Authentication, running circuits, reading results |
| [API Reference](api.md) | `QProvider`, `QBackend`, `QJob`, `QException` |
| [Architecture](architecture.md) | Layer design and HEAppE execution flow |

## Authors
- Jan Swiatkowski, jan.swiatkowski@vsb.cz
- Ladislav Foltyn, ladislav.foltyn@vsb.cz
- Ada Böhm, ada.bohm@vsb.cz
- Jakub Konvička, jakub.konvicka@vsb.cz


## License

Apache 2.0 — see `LICENSE` file.