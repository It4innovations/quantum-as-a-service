# Docker install smoke test

Checks that `qaas[iqm_qiskit]` installs cleanly in a fresh Python 3.12 container and
that `qheappe_client` can connect to a real backend and submit jobs. Useful before and after a release to catch packaging problems
(missing dependencies, wrong index, files missing from the wheel) that don't show up
in a development environment.

Two flows, both running the same test script ([smoke_submit.py](smoke_submit.py)):

| Script | Installs `qaas` from | Use it to |
|---|---|---|
| [run.sh](run.sh) | package index (as documented in the main README) | verify a published release |
| [run_local_dist.sh](run_local_dist.sh) | a wheel built from your working copy | verify a release candidate before publishing |

No repository sources are copied into the containers - only the installed package is tested.

## What the test does

1. Imports `qheappe_client` (fails here if the `iqm_qiskit` extra is missing) and prints
   the installed versions of `qaas`, `qiskit`, `iqm-client`, `iqm-pulla`, `iqm-exa-common`
   and `py4lexis`.
2. Connects with `QProvider`, gets the backend and its architecture, and a `QPulla` client
   (no allocation used).
3. Unless `SKIP_QPU=1`: runs a Bell circuit with `backend.run()` (qiskit `Result`) and with
   `QSamplerV2` (`PrimitiveResult`), and checks that the counts sum to `SHOTS`.

It prints `SMOKE TEST PASSED` at the end; any failure exits non-zero.

> **Note:** each run submits 2 circuit jobs and consumes allocation of the given LEXIS
> project. Set `SKIP_QPU=1` to check installation, imports and connection only.

## Requirements

- Docker running locally
- A valid LEXIS access token for the project
- For `run_local_dist.sh`: a local Python with `pip` (set `PYTHON=...` to choose one)

## Configuration

Copy the example env file in the repository root and fill it in:

```bash
cp .env_install_test.example .env_install_test
```

| Variable | Required | Description |
|---|---|---|
| `ACCESS_TOKEN` (or `LEXIS_TOKEN`) | yes | LEXIS access token |
| `LEXIS_PROJECT` (or `QHEAPPE_PROJECT`) | yes | LEXIS project short name |
| `RESOURCE_NAME` (or `QHEAPPE_RESOURCE`) | yes | LEXIS quantum resource, e.g. `VLQ-EU` |
| `BACKEND_NAME` (or `QHEAPPE_BACKEND`) | no | backend (cluster) name, e.g. `VLQ` |
| `LEXIS_API_URL` (or `QHEAPPE_LEXIS_API_URL`) | no | LEXIS API base URL |
| `SHOTS` | no | shots per job, default `100` |
| `SKIP_QPU` | no | `1`: submit no jobs (also read from the shell by the run scripts) |

The names in brackets are those of the live test suite (`tests/qheappe/settings.py`).

Write values without quotes - `docker --env-file` passes them literally, so a quoted
token fails with `JWT token decode error`. (The repository `.env` read by pytest may use
quotes, so it cannot always be passed to these scripts as is.)
`.env_install_test` is git-ignored; never commit it. The token is passed to the container
only at runtime (not baked into the image), and the container is removed after the run.

## Usage

Run from the repository root.

```bash
# Released package (default: newest release)
tests/docker_install_package/run.sh
tests/docker_install_package/run.sh -v 1.1.0        # specific version
tests/docker_install_package/run.sh path/to/other.env

# Wheel built from the working copy
tests/docker_install_package/run_local_dist.sh
tests/docker_install_package/run_local_dist.sh path/to/other.env
```

The version can also be set with `QAAS_VERSION=1.1.0`.

Install, import and connect only, without submitting jobs:

```bash
SKIP_QPU=1 tests/docker_install_package/run_local_dist.sh
```

`run_local_dist.sh` builds the wheel from a clean temporary copy of `pyproject.toml`,
`README.rst`, `LICENSE` and `src/`, so stale `build/` or `dist/` folders in the repo
can't leak into it.

## Install notes

- `py4lexis` is not on PyPI, so `qaas` must be installed with the extra index:
  `pip install "qaas[iqm_qiskit]" --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple`
- `git` package must be available: `py4lexis` 7.1.x pulls `python-http-irods-client` from a git URL.
  The images install it for this reason.

## Troubleshooting

- **`Env file not found`** - create `.env_install_test` (see Configuration) or pass a path.
- **`JWT token has expired`** - get a new LEXIS token and update the env file.
