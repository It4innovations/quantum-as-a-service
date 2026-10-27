---
icon: lucide/layers
---

# Architecture

## Layer design

QaaS is structured in the following manner:

```mermaid
graph TD
    User["User code\n(Qiskit circuits)"]
    P["Provider layer\nprovider.py"]
    C["Client layer\nclient.py"]
    B["Backend layer\nbackend_iqm.py"]
    HEAppE["HEAppE Middleware"]
    QScheduler["QScheduler"]
    IQM["IQM hardware"]

    User --> P
    P --> C
    P --> B
    C --> HEAppE
    HEAppE --> QScheduler
    B --> IQM
    QScheduler --> IQM
```

### Provider layer (`provider.py`)

Authenticates against the LEXIS platform using the supplied token and project name, then resolves the requested resource to a concrete backend implementation. This is the only layer the user interacts with directly.

### Client layer (`client.py`)

Handles HEAppE job lifecycle: submission, polling, result retrieval, and error propagation back to the caller. Internally manages encrypted credentials and accounting information.

### Backend layer (`backend_iqm.py`)

Creates IQM-specific `QBackend` instances. Fetches real-time hardware calibration data, drives circuit transpilation through the IQM gate set, and wraps raw IQM results into the `QJob`/result abstraction.

## HEAppE/QScheduler execution flow

Jobs travel through the following steps once `backend.run()` is called:

1. **Create Quantum Job** — quantum circuit and parameters are packaged into a Quantum Job via HEAppE.
2. **Submission** — the quantum circuit is being submitted to QScheduler.
3. **QScheduler**  — QScheduler collects Quantum jobs to be submitted and dispatches them to the IQM hardware in batches.
4. **Result retrieval** — measurement counts are collected by QScheduler and streamed back via HEAppE to the QaaS client, which surfaces them by `job.result().get_counts()` method.

## Key design decisions

- **Hardware-specific transpilation** is performed client-side before submission, so that the backend always receives a valid native-gate circuit.
- **Real-time calibration** is fetched at backend creation time, ensuring transpilation targets the current hardware state.
- **IQM pulse-level optimisation** (`iqm-pulla`) is available as an optional post-transpilation step for advanced users.
