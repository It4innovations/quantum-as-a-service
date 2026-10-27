---
icon: lucide/package
---

# Installation

### Requirements

- Python **3.12** (exactly: `>=3.12,<3.13`)
- `pip >= 26.0`

### Optional
- Git

A virtual environment (`venv` or `uv`) is strongly recommended and fully supported.

## Install py4lexis

`py4lexis` handles user's authentication via the LEXIS Platform and has to be installed first:

```bash
pip install --index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple py4lexis
```

## Install QaaS

#### From PyPI

```bash
pip install qaas
```

#### From GitHub (stable)

```bash
pip install git+https://github.com/It4innovations/quantum-as-a-service.git@main
```

#### For development/contributor purposes (editable install)

```bash
git clone https://github.com/It4innovations/quantum-as-a-service.git
cd quantum-as-a-service
pip install -e .
```

## Optional extras

The `iqm_backend` extra is needed to run the backend service directly on IQM hardware nodes

```bash
pip install "qaas[iqm_backend]"
```

## Dependency overview

| Package | Version | Purpose |
|---------|---------|---------|
| `qiskit` | 1.4.5 | Quantum circuit framework |
| `iqm-client[qiskit]` | 33.0.* | IQM quantum interface |
| `iqm-pulla` | 12.0.* | Pulse-level optimisation |
| `qiskit_aer` | >=0.15,<1.0 | Circuit simulation |
| `Py4HEAppE` | >=2.5.0 | HEAppE job management |
| `numpy` | >2.0.0 | Numerical processing |
| `cryptography` | >=43.0.0 | Cryptographic operations |
| `PyJWT[crypto]` | 2.10.* | JSON Web Token handling |
