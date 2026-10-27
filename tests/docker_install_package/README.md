# Docker install smoke test

Checks that `qaas` installs cleanly in a fresh Python 3.12 container and can submit
jobs to a real backend. Useful before and after a release to catch packaging problems
(missing dependencies, wrong index, files missing from the wheel) that don't show up
in a development environment.

Two flows, both running the same test script ([smoke_submit.py](smoke_submit.py)):

| Script | Installs `qaas` from | Use it to |
|---|---|---|
| [run.sh](run.sh) | package index (as documented in the main README) | verify a published release |
| [run_local_dist.sh](run_local_dist.sh) | a wheel built from your working copy | verify a release candidate before publishing |

No repository sources are copied into the containers - only the installed package is tested.

## What the test does

Mirrors [randomnumbers_Qprovider.ipynb](../../src/qaas/client/example/randomnumbers_Qprovider.ipynb):

1. Connects with `QProvider(LEXIS_PROJECT, ACCESS_TOKEN)` and gets the backend `RESOURCE_NAME`.
2. Builds a 15-qubit circuit and transpiles it with `transpile_to_IQM`.
3. Submits it as a Qiskit circuit, then again as OpenQASM 3.
4. Checks that each job returns counts summing to `SHOTS`.

It prints `SMOKE TEST PASSED` at the end; any failure exits non-zero.

> **Note:** each run submits real jobs (backend init + 2 circuit jobs) and consumes
> allocation of the given LEXIS project.

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
| `ACCESS_TOKEN` | yes | LEXIS access token |
| `LEXIS_PROJECT` | yes | LEXIS project name |
| `RESOURCE_NAME` | yes | resource name, e.g. `VLQ-EU` |
| `SHOTS` | no | shots per job, default `100` |

Write values without quotes - `docker --env-file` passes them literally.
`.env_install_test` is git-ignored; never commit it. The token is passed to the container
only at runtime (not baked into the image), and the container is removed after the run.

## Usage

Run from the repository root.

```bash
# Released package (default: newest release)
tests/docker_install_package/run.sh
tests/docker_install_package/run.sh -v 0.5.2        # specific version
tests/docker_install_package/run.sh path/to/other.env

# Wheel built from the working copy
tests/docker_install_package/run_local_dist.sh
tests/docker_install_package/run_local_dist.sh path/to/other.env
```

The version can also be set with `QAAS_VERSION=0.5.2`.

`run_local_dist.sh` builds the wheel from a clean temporary copy of `pyproject.toml`,
`README.rst`, `LICENSE` and `src/`, so stale `build/` or `dist/` folders in the repo
can't leak into it.

## Install notes

- `py4lexis` is not on PyPI, so `qaas` must be installed with the extra index:
  `pip install qaas --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple`
- `git` package must be available: `py4lexis` 7.1.x pulls `python-http-irods-client` from a git URL.
  The images install it for this reason.

## Troubleshooting

- **`Env file not found`** - create `.env_install_test` (see Configuration) or pass a path.
