---
icon: lucide/package
---

# Installation

## Requirements

- Python **3.12** (`>=3.12,<3.13`)
- A LEXIS Platform project with an allocated quantum resource (see [Usage](usage.md))

A virtual environment (`venv` or `uv`) is strongly recommended.

## Install the QHEAppE Client

The QHEAppE Client is published in the `qaas` package on the IT4Innovations package index. Install it together with **one** IQM extra:

```bash
pip install 'qaas[iqm_qiskit]' --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple
```

The `--extra-index-url` is required: it also provides `py4lexis`, which handles LEXIS authentication and is installed automatically.

`1.0.0a1` is a pre-release. To pin it, put the extra before the version:

```bash
pip install 'qaas[iqm_qiskit]==1.0.0a1' --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple
```

In a Jupyter notebook use `%pip install ...` with the same arguments and restart the kernel afterwards.

!!! warning "Install an IQM extra"
    `pip install qaas` alone does **not** install Qiskit or the IQM libraries. Importing `qheappe_client` without them raises an `ImportError` that prints the install command.

## IQM extras and backend compatibility

Install exactly one of the extras. They pin different, incompatible versions of the IQM libraries.

| Extra | IQM libraries | Works with backends on station-control | Use it when |
|-------|---------------|----------------------------------------|-------------|
| `iqm_qiskit` (alias `iqm36_qiskit`) | iqm-client 36.x | **50.x** (current) and **49.x** (older) | Always, unless you need Pulla — **recommended** |
| `iqm35_qiskit` | iqm-client 35.x | 49.x only | You need Pulla (pulse-level access) |

When you call `QProvider.get_backend()`, the client reads the station-control version the backend reports and checks that the installed IQM libraries work with it. If they don't (for example `iqm35_qiskit` against a 50.x backend), you get a `QException` with the command for the right extra. Pass `strict_iqm_version=False` to get a warning instead.

!!! note "Pulla is currently not available"
    With `iqm_qiskit` (iqm-client 36.x), Pulla is not available: iqm-pulla 15 removed the `Pulla` class. `QProvider.get_pulla()` raises `NotImplementedError`. Pulse-level work needs `qaas[iqm35_qiskit]` and a station-control 49.x backend.

## Development install

```bash
git clone https://github.com/It4innovations/quantum-as-a-service.git
cd quantum-as-a-service
pip install -e '.[iqm_qiskit,testenv]' --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple
```

`testenv` adds `pytest`, `pytest-dotenv` and `pytest-timeout`. Run the offline tests with:

```bash
python -m pytest tests/qheappe/test_unit.py tests/qheappe/test_iqm_compat.py
```

The live tests (`tests/qheappe/test_integration.py`) need a LEXIS token and project settings; see `.env.test.example`.

## Main dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `py4lexis` | 7.1.* | LEXIS Platform authentication |
| `Py4HEAppE` | 3.1.* | HEAppE API client |
| `aiohttp` | 3.14.* | Async HTTP and job-event websocket |
| `PyJWT[crypto]` | 2.10.* | LEXIS token verification |
| `qiskit` | >=2.0,<2.2 | Circuits and results (from the IQM extra) |
| `iqm-client[qiskit]` | 36.0.* / 35.0.* | IQM transpilation and job definitions (from the IQM extra) |

The full list is in `pyproject.toml`.

## Legacy `qaas` client

The repository also contains the previous client (`qaas.client`), which runs jobs through HEAppE command templates and is built on Pulla. It only works with `qaas[iqm35_qiskit]`. New code should use the QHEAppE Client (`qheappe_client`).
