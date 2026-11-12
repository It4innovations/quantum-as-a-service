# Changelog

All notable changes to `qaas` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with
[PEP 440](https://peps.python.org/pep-0440/) pre-release suffixes (`a1`, `b1`, `rc1`).

## [1.1.0a1] - Unreleased

Makes `qheappe_client` backends follow the Qiskit `BackendV2` / `JobV1` contract, so
`BackendSamplerV2`, `BackendEstimatorV2`, `qiskit-experiments` and other tools that take
a backend work unchanged ([#49]).

### Changed

- **Breaking:** `QJob.result()` always returns a `qiskit.result.Result`, as
  `JobV1.result()` requires. Code that reads `job.result()[0].data.meas` needs
  `QSamplerV2` (below).

### Added

- `QSamplerV2`, a QaaS sampler primitive (`BaseSamplerV2`) whose jobs return a
  `PrimitiveResult`. It replaces `PrimitiveResult` output on `QJob`. Each PUB result
  has one `BitArray` per classical register, read from the IQM measurement keys, and
  keeps the shape of the PUB's parameter values. PUBs with different shot counts run
  as separate HEAppE jobs.
- `QSamplerJob`, the job `QSamplerV2.run()` returns, with `status()`, `cancel()` and
  `jobs` (the underlying `QJob`s).
- `QJob.wait_for_final_state(timeout=None, wait=5, callback=None)` follows HEAppE's
  event websocket instead of polling `status()` every `wait` seconds, with the same
  interface and `JobTimeoutError` as Qiskit's. The job info is still fetched every
  `wait` seconds as a backup, for state changes made before the websocket connected
  or when it fails. `QJob.result()` waits the same way.
- `QSamplerJob.wait_for_final_state(...)`, the same for all backend jobs of a sampler
  job, with a shared timeout.

### Fixed

- **Breaking:** `QJob.result()` bitstrings now follow the Qiskit bit order (clbit 0
  rightmost, registers in circuit order), as documented. Before, they were reversed:
  `x(0)` on 3 qubits read `100` instead of `001`, so `BackendSamplerV2` assigned bits to
  the wrong registers. Bits are now placed using the IQM measurement keys.
- **Breaking:** `QJob.result()` keeps the circuit's classical registers, read from the
  IQM measurement keys. Counts and memory of a circuit with several registers read
  e.g. `"0 01"` (registers separated by a space), as in Qiskit, `IQMBackend` and the
  legacy `qaas.client`; before, `"001"`. Circuits with one register (e.g.
  `measure_all()`) are unchanged.
- `QJob.result()` names each experiment after its circuit and includes the circuit's
  metadata, as Qiskit backends do, so `result.get_counts(circuit)` works. Before,
  experiments were named `circuit-0`, … without metadata. A job re-created with
  `QJob(backend, job_id)` still uses `circuit-<i>` names and empty metadata, because
  the circuits are not available then.
- `QJob.cancel()` now sends the cancel request to HEAppE and returns whether it was
  accepted. Before, the request was never sent.
- `QJob.status()` after a cancel returns `JobStatus.CANCELLED`. Before, it raised
  `AttributeError` (it used `JobStatus.CANCELED`, which Qiskit does not define).
- `backend.run(circuits)` without `shots` uses `backend.options.shots` (default 1024),
  as `BackendV2.run()` expects. Before, it raised `TypeError`.
- `QJob.result()` no longer hangs on a job that finished before the wait started. It
  used the job state from submission time and waited on the HEAppE event stream, which
  does not report state changes that happened before it opened. It now reuses a final
  state already seen by `status()`, otherwise asks HEAppE first, and refreshes the job
  info after waiting.
- `backend.transpile()` keeps each circuit's metadata. `transpile_to_IQM` dropped it on
  backends with a resonator (e.g. VLQ), so results lost the circuit metadata.
- `QProvider.list_available_backends()` connects its client (and closes it afterwards).
  Before, it raised `QAuthException` because the client was never authenticated.
- A failed LEXIS resource lookup (e.g. HTTP 503) raises `QAuthException` ("Failed to
  authorize LEXIS resource: ..."). Before, a wrong internal call raised `TypeError`.

### Removed

- **Breaking:** the `qiskit_legacy` argument of `QJob.result()`. Drop
  `qiskit_legacy=True`; replace `qiskit_legacy=False` with `QSamplerV2`, or
  `qiskit.primitives.BackendSamplerV2(backend)`.
- **Breaking:** the `key_order_left_to_right` argument of `QJob.result()`,
  `result_to_qiskit()` and `result_to_qiskit_legacy()`. Bitstrings always follow the
  Qiskit order; reverse them yourself (`bits[::-1]`) if you need the opposite.
- **Breaking:** the `with_measurements` argument of `QJob.result()`, which now takes no
  arguments, as `JobV1.result()`. Per-shot results come from
  `backend.run(..., memory=True)`, or `QJob(backend, job_id, memory=True)` for an
  earlier job.

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
[1.1.0a1]: https://github.com/It4innovations/quantum-as-a-service/compare/v1.0.1a1...HEAD
[1.0.1a1]: https://github.com/It4innovations/quantum-as-a-service/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/It4innovations/quantum-as-a-service/releases/tag/v1.0.0
