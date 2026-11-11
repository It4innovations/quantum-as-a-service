# Changelog

All notable changes to `qaas` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with
[PEP 440](https://peps.python.org/pep-0440/) pre-release suffixes (`a1`, `b1`, `rc1`).

## [1.1.0] - Planned

Makes `qheappe_client` backends follow the Qiskit `BackendV2` / `JobV1` contract, so
`BackendSamplerV2`, `BackendEstimatorV2`, `qiskit-experiments` and other tools that take
a backend work unchanged ([#49]).

### Changed

- **Breaking:** `QJob.result()` returns a `qiskit.result.Result` by default
  (`qiskit_legacy=True`), as `JobV1.result()` requires. Code that reads
  `job.result()[0].data.meas` needs `QSamplerV2` (below) or
  `job.result(qiskit_legacy=False)`.

### Added

- `QSamplerV2`, a QaaS sampler primitive (`BaseSamplerV2`) whose jobs return a
  `PrimitiveResult`. It replaces `PrimitiveResult` output on `QJob`.

### Deprecated

- `QJob.result(qiskit_legacy=False)` emits a `DeprecationWarning`. Use `QSamplerV2`, or
  `qiskit.primitives.BackendSamplerV2(backend)`, for primitive-style results.
- The `qiskit_legacy` argument of `get_backend()`.

## [1.0.1a1] - Unreleased

### Fixed

- `backend.run(..., memory=True)` is now honored: the job downloads per-shot
  measurements, and `result()` includes them (`Result.get_memory()`,
  `PrimitiveResult[i].data.meas.get_bitstrings()`). Before, `memory` was ignored, so
  `BackendSamplerV2` got no per-shot data and reported all-zero bitstrings
  (e.g. `{'00': 1024}` for a Bell state) ([#49]).
- `QJob.status()` queries HEAppE again, as `qaas.client` did, until the job reaches a
  terminal state. Before, it returned a cached status that only `result()` updated, so
  `status()`, `done()` and `in_final_state()` reported `QUEUED` until `result()` was
  called, and `wait_for_final_state()` never returned.
- HEAppE `Queued` and `WaitingForServiceAccount` states map to `JobStatus.QUEUED`.
  Before, they mapped to `JobStatus.ERROR`.

### Changed

- `QJob.result(with_measurements=...)` now defaults to `None`, which follows the
  `memory` option given to `backend.run()` (default `False`). An explicit `True` or
  `False` still overrides it, so existing calls behave as before.
- `QBackend` default options now include `memory=False`.

## [1.0.0] - 2026-11-06

First stable release.

[#49]: https://github.com/It4innovations/quantum-as-a-service/issues/49
[1.1.0]: https://github.com/It4innovations/quantum-as-a-service/compare/v1.0.1a1...HEAD
[1.0.1a1]: https://github.com/It4innovations/quantum-as-a-service/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/It4innovations/quantum-as-a-service/releases/tag/v1.0.0
