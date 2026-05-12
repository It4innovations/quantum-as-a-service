---
icon: lucide/atom
---

# QaaS — Quantum-as-a-Service

**QaaS** provides access to quantum computing hardware through the [LEXIS platform](https://lexis-project.eu/) via HEAppE. It targets IQM quantum hardware and aims to offer a vendor-neutral interface for academic quantum infrastructure.

!!! note "Documentation"
    Full documentation is available at **[it4innovations.github.io/quantum-as-a-service](https://it4innovations.github.io/quantum-as-a-service/)**.

## Quick start

```python
from py4lexis.session import LexisSession
from qaas import QProvider
from qiskit import QuantumCircuit

# Authenticate
token = LexisSession().get_access_token()

# Connect to hardware
provider = QProvider(token, "my_lexis_project")
backend  = provider.get_backend("EQE1-CZ-P0001")

# Run a Bell-state circuit
qc = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure_all()

job    = backend.run(backend.transpile(qc), shots=1000)
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
