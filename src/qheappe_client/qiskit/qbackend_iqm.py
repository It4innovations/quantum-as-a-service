"""Qiskit backend for IQM hardware behind HEAppE/QScheduler.

This module only uses IQM symbols that exist in every supported IQM stack
(iqm-client 35.x and 36.x), so it imports with either one installed.
Version-specific behaviour lives in :mod:`qheappe_client.iqm_compat`; use
:class:`~qheappe_client.iqm_compat.QBackendIQMFactory` (or
:meth:`QProvider.get_backend`) to get the backend class that matches the
installed stack and the server.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

import numpy as np
from exa.common.errors.iqm_error import CircuitValidationError
from iqm.iqm_client import CircuitCompilationOptions, DynamicQuantumArchitecture
from iqm.iqm_client.models import validate_circuit
from iqm.iqm_client.validation import (
    validate_circuit_instructions,
    validate_qubit_mapping,
)
from iqm.qiskit_iqm import transpile_to_IQM
from iqm.qiskit_iqm.iqm_backend import IQMBackendBase
from iqm.qiskit_iqm.iqm_provider import IQMBackend
from iqm.station_control.client.qon import ObservationFinder
from iqm.station_control.interface.models import (
    CircuitBatch,
    ObservationLite,
    QIRCode,
    QubitMapping,
)

# PostJobsRequest is the wire model in both stacks: called RunRequest in 35.x
# and CircuitJobDefinition in 36.x (both are aliases of it).
# _Circuit is the obsolete circuit type.
from iqm.station_control.interface.models.circuit import PostJobsRequest, _Circuit
from qiskit import QuantumCircuit
from qiskit.primitives import PrimitiveResult, SamplerPubResult
from qiskit.primitives.containers import BitArray, DataBin
from qiskit.result import Result
from qiskit.result.models import ExperimentResult, ExperimentResultData

from qheappe_client.backend_metadata import QBackendMetadata
from qheappe_client.qclient import QClient
from qheappe_client.qiskit.qbackend import QBackend
from qheappe_client.utils import is_valid_uuid

if TYPE_CHECKING:
    from qheappe_client.iqm_compat.stack import IQMStack
    from qheappe_client.qpulla import QPulla

__all__ = ["CircuitJobDefinition", "QBackendIQM", "transpile_to_IQM"]

CircuitJobDefinition = PostJobsRequest

# IQM measurement key: "<creg name>_<creg size>_<creg index>_<clbit index>"
# (see iqm.qiskit_iqm.qiskit_to_iqm.MeasurementKey).
_IQM_MEAS_KEY = re.compile(r"^(.*)_(\d+)_(\d+)_(\d+)$")
_QISKIT_ORDER_KEY = "__qiskit_order__"


def _to_qiskit_order(
    item: dict[str, Any], measurements: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Reorder one circuit's results into a single Qiskit-ordered bitstring.

    The server returns one bit per IQM measurement key, in key order. Each key names
    its classical register and bit, so the bits are placed where Qiskit expects them:
    registers in circuit order, clbit 0 rightmost (as ``IQMJob`` does). Returns the
    inputs unchanged if the keys are not IQM measurement keys.

    Args:
        item: ``{"measurement_keys": [...], "counts": {bitstring: n}}`` of one circuit.
        measurements: ``{key: [[bit], ...shots]}`` of the same circuit, or ``None``.

    Returns:
        ``(item, measurements)`` with a single key whose bitstrings read most
        significant clbit first, or the inputs unchanged.
    """
    keys = item["measurement_keys"]
    parsed = [_IQM_MEAS_KEY.match(k) for k in keys]
    if not keys or not all(parsed):
        return item, measurements

    sizes = {int(m.group(3)): int(m.group(2)) for m in parsed}
    offsets, width = {}, 0
    for creg_idx in sorted(sizes):
        offsets[creg_idx] = width
        width += sizes[creg_idx]
    clbits = [offsets[int(m.group(3))] + int(m.group(4)) for m in parsed]
    cols = [width - 1 - c for c in clbits]  # string position, MSB first
    counts = item.get("counts", {})
    if (
        any(int(m.group(4)) >= int(m.group(2)) for m in parsed)
        or len(set(cols)) != len(cols)
        or any(len(bs) != len(keys) for bs in counts)
    ):
        return item, measurements

    def reorder(bits: str) -> str:
        out = ["0"] * width
        for col, bit in zip(cols, bits, strict=True):
            out[col] = bit
        return "".join(out)

    new_counts: dict[str, int] = {}
    for bs, n in counts.items():
        key = reorder(bs)
        new_counts[key] = new_counts.get(key, 0) + n

    new_measurements = None
    if measurements is not None:
        arrays = [
            np.asarray(measurements[k], dtype=int).reshape(len(measurements[k]), -1)
            for k in keys
        ]
        if any(a.shape[1] != 1 for a in arrays) or len({len(a) for a in arrays}) != 1:
            return item, measurements
        full = np.zeros((len(arrays[0]), width), dtype=int)
        for col, a in zip(cols, arrays, strict=True):
            full[:, col] = a[:, 0]
        new_measurements = {_QISKIT_ORDER_KEY: full.tolist()}

    return {
        **item,
        "measurement_keys": [_QISKIT_ORDER_KEY],
        "counts": new_counts,
    }, new_measurements


def _iqm_creg_sizes(keys: list[str]) -> list[list[str | int]] | None:
    """Classical registers named by IQM measurement keys, in circuit order.

    Args:
        keys: IQM measurement keys, ``<creg>_<size>_<creg index>_<clbit index>``.

    Returns:
        ``[[name, size], ...]`` ordered by register index, as Qiskit's ``creg_sizes``
        result header; ``None`` if a key is not an IQM measurement key. Registers that
        no measurement writes to are not listed (their keys never reach the server).
    """
    regs: dict[int, list[str | int]] = {}
    for key in keys:
        m = _IQM_MEAS_KEY.match(key)
        if m is None:
            return None
        regs[int(m.group(3))] = [m.group(1), int(m.group(2))]
    return [regs[i] for i in sorted(regs)] or None


class QBackendIQM(QBackend, IQMBackend):
    """QaaS backend for IQM hardware. Target/qubit mapping come from IQMBackendBase,
    execution goes through QClient/HEAppE.

    Works with every supported IQM stack. Stack-specific subclasses
    (``QBackendIQM35``, ``QBackendIQM36``) set :attr:`iqm_stack` and override
    the hooks below where the IQM libraries differ.
    """

    #: IQM stack this class targets; ``None`` for the version-agnostic base.
    iqm_stack: ClassVar[IQMStack | None] = None

    @staticmethod
    def _load_metrics(metrics: dict[str, Any]) -> ObservationFinder:
        """Build an ObservationFinder from a ``MachineCalibration`` metrics response.

        Args:
            metrics: Parsed metrics JSON with an ``observations`` list.

        Returns:
            ObservationFinder over the observations.
        """
        observations = [
            ObservationLite.model_validate(o) for o in metrics.get("observations", [])
        ]
        return ObservationFinder(observations)

    def __init__(
        self,
        client: QClient,
        architecture: DynamicQuantumArchitecture,
        name: str = "QBackendIQM",
        backend_metadata: QBackendMetadata = None,
        calibration_set_id: str | None = None,
        use_metrics: bool = False,
        **kwargs,
    ):
        QBackend.__init__(
            self, client=client, architecture=architecture, name=name, **kwargs
        )

        metrics = (
            self._load_metrics(self._qclient.get_machine_metrics())
            if use_metrics
            else None
        )

        IQMBackendBase.__init__(
            self, architecture=architecture, name=name, metrics=metrics, **kwargs
        )

        self._client = None  # type: ignore[assignment]  # IQMBackend expects a different client type; we don't use it.

        self._use_default_calibration_set = calibration_set_id is None
        self._backend_metadata = backend_metadata
        self._architecture = architecture
        self._max_circuits: int | None = None
        self._calibration_set_id = architecture.calibration_set_id

    @property
    def client(self) -> QPulla:
        """IQM client of this backend, connected through HEAppE (pulse-level access).

        A :class:`~qheappe_client.qpulla.QPulla`, created on first use, so IQM's
        pulse-level helpers that take a backend work with this one too, e.g.
        ``iqm.qiskit_iqm.pulse_level_utils.qiskit_to_iqm(backend, circuits)``.
        Needs the IQM 36 stack.
        """
        if getattr(self, "_iqm_client", None) is None:
            from qheappe_client.qpulla import QPulla

            self._iqm_client = QPulla(self._qclient, backend=self)
        return self._iqm_client

    @client.setter
    def client(self, value: QPulla) -> None:
        self._iqm_client = value

    @property
    def max_circuits(self) -> int | None:
        return None

    def result_to_qiskit(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
    ) -> PrimitiveResult:
        """
        Convert raw results to a Qiskit PrimitiveResult object.

        Args:
            raw: A list of dictionaries containing measurement keys and counts.
                 Each dictionary should have the form:
                 {"measurement_keys": [...], "counts": {...}}
            measurements_raw: Optional, one dictionary per PUB containing raw measurements.
                              If provided, used instead of counts to preserve shot order.
                              Format: {key: [[bit, ...], ...shots]}
            reg_name: The name of the classical register to use for measurement results.

        Bitstrings follow the Qiskit order (registers in circuit order, clbit 0
        rightmost), using the IQM measurement keys. Results with other keys are
        read as given, most significant bit first.

        Returns:
            A PrimitiveResult object containing the processed measurement results.
        Raises:
            ValueError: If the number of raw results does not match the number of measurement sets.
            ValueError: If the shot counts differ between keys in a PUB.
            ValueError: If the number of shots in measurements does not match the sum of counts.
        """
        if measurements_raw is not None and len(measurements_raw) != len(raw):
            raise ValueError(
                f"{len(raw)} results but {len(measurements_raw)} measurement sets"
            )

        pubs = []
        for i, item in enumerate(raw):
            keys = item["measurement_keys"]
            item, m = _to_qiskit_order(
                item, measurements_raw[i] if measurements_raw is not None else None
            )
            read_keys = item["measurement_keys"]

            if m is not None:
                cols = [
                    np.asarray(m[k], dtype=bool).reshape(len(m[k]), -1)
                    for k in read_keys
                ]
                if len({c.shape[0] for c in cols}) != 1:
                    raise ValueError(f"PUB {i}: keys have different shot counts")
                bitarray = BitArray.from_bool_array(
                    np.concatenate(cols, axis=1), order="big"
                )

                if "counts" in item and bitarray.num_shots != sum(
                    item["counts"].values()
                ):
                    raise ValueError(f"PUB {i}: shots in measurements != sum of counts")
            else:
                n = len(next(iter(item["counts"])))
                int_counts = {int(bs, 2): c for bs, c in item["counts"].items()}
                bitarray = BitArray.from_counts(int_counts, num_bits=n)

            data = DataBin(**{reg_name: bitarray}, shape=bitarray.shape)
            pubs.append(SamplerPubResult(data, metadata={"measurement_keys": keys}))
        return PrimitiveResult(pubs, metadata={})

    def result_to_qiskit_legacy(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
        backend_name: str = "my_backend",
        job_id: str = "",
        circuit_headers: list[dict[str, Any]] | None = None,
    ) -> Result:
        """
        Convert raw results to a Qiskit Result object in legacy format.

        Args:
            raw: A list of dictionaries containing measurement keys and counts.
                 Each dictionary should have the form:
                 {"measurement_keys": [...], "counts": {...}}
            measurements: Optional, one dictionary per PUB containing raw measurements.
                          If provided, used instead of counts to preserve shot order.
                          Format: {key: [[bit, ...], ...shots]}
            reg_name: The name of the classical register to use for measurement results.
            backend_name: The name of the backend used for the execution.
            job_id: The ID of the job that produced the results.
            circuit_headers: Optional, one ``{"name": ..., "metadata": ...}`` per circuit,
                copied into each experiment's header (so ``get_counts(circuit)`` works).
                Experiments are named ``circuit-<i>`` without it.

        Bitstrings follow the Qiskit order (registers in circuit order, clbit 0
        rightmost), using the IQM measurement keys. The IQM keys also give the
        classical registers, so counts read e.g. ``"0 01"`` for registers ``a`` (2 bits)
        and ``b`` (1 bit), as in Qiskit. Results with other keys are read as given,
        most significant bit first, as one register ``reg_name``.

        Returns:
            A Result object containing the processed measurement results.

        Raises:
            ValueError: If the number of raw results does not match the number of measurement sets.
        """

        if circuit_headers is not None and len(circuit_headers) != len(raw):
            circuit_headers = None  # e.g. a job re-created with different circuits

        exps = []
        for i, item in enumerate(raw):
            keys = item["measurement_keys"]
            creg_sizes = _iqm_creg_sizes(keys)
            info = circuit_headers[i] if circuit_headers else {}
            item, m = _to_qiskit_order(
                item, measurements_raw[i] if measurements_raw is not None else None
            )
            counts = {hex(int(bs, 2)): c for bs, c in item["counts"].items()}
            n = len(next(iter(item["counts"])))

            memory = None
            if m is not None:
                per_key = [m[k] for k in item["measurement_keys"]]  # [key][shot][bit]
                shots = len(per_key[0])
                bitstrs = [
                    "".join(str(int(b)) for k in per_key for b in k[s])
                    for s in range(shots)
                ]
                memory = [hex(int(s, 2)) for s in bitstrs]

            exps.append(
                ExperimentResult(
                    shots=sum(item["counts"].values()),
                    success=True,
                    data=ExperimentResultData(counts=counts, memory=memory),
                    header={
                        "name": info.get("name") or f"circuit-{i}",
                        "creg_sizes": creg_sizes or [[reg_name, n]],
                        "memory_slots": n,
                        "metadata": info.get("metadata") or {},
                        "measurement_keys": keys,
                    },
                )
            )

        return Result(
            backend_name=backend_name,
            backend_version="1.0",
            job_id=job_id,
            success=True,
            results=exps,
        )

    def create_run_request(
        self,
        run_input: QuantumCircuit | list[QuantumCircuit],
        shots: int = 1024,
        circuit_compilation_options: CircuitCompilationOptions | None = None,
        circuit_callback: Callable[[list[QuantumCircuit]], Any] | None = None,
        qubit_index_to_name: dict[int, str] | None = None,
        **unknown_options,
    ) -> CircuitJobDefinition:
        """Alias of :meth:`create_circuit_job_definition` (iqm-client 35.x name)."""
        return self.create_circuit_job_definition(
            run_input,
            shots=shots,
            circuit_compilation_options=circuit_compilation_options,
            circuit_callback=circuit_callback,
            qubit_index_to_name=qubit_index_to_name,
            **unknown_options,
        )

    def create_circuit_job_definition(
        self,
        run_input: QuantumCircuit | list[QuantumCircuit],
        shots: int = 1024,
        circuit_compilation_options: CircuitCompilationOptions | None = None,
        circuit_callback: Callable[[list[QuantumCircuit]], Any] | None = None,
        qubit_index_to_name: dict[int, str] | None = None,
        **unknown_options,
    ) -> CircuitJobDefinition:
        """Create a circuit job definition (run request) without submitting it for execution.

        This can be used to check what would be submitted for execution by an equivalent call to :meth:`run`.

        Args:
            run_input: Same as in :meth:`run`.

        Args:
            shots: Number of repetitions of each circuit, for sampling.
            circuit_compilation_options:
                Compilation options for the circuits, passed on to :class:`~iqm.iqm_client.iqm_client.IQMClient`.
                If ``None``, the defaults of the :class:`~iqm.iqm_client.models.CircuitCompilationOptions`
                class are used.
            circuit_callback:
                Callback function that, if provided, will be called for the circuits before sending
                them to the device.  This may be useful in situations when you do not have explicit
                control over transpilation, but need some information on how it was done. This can
                happen, for example, when you use pre-implemented algorithms and experiments in
                Qiskit, where the implementation of the said algorithm or experiment takes care of
                delivering correctly transpiled circuits to the backend. This callback method gives
                you a chance to look into those transpiled circuits, and extract any info you need.
                As a side effect, you can also use this callback to modify the transpiled circuits
                in-place, just before execution; however, we do not recommend to use it for this
                purpose.
            qubit_index_to_name: Mapping from qubit indices in the circuit to qubit names on the device.
                If ``None``, :attr:`.IQMBackendBase.index_to_qubit_name` will be used.
            unknown_options: Backwards compatibility for deprecated parameters.

        Returns:
            The created circuit job definition.

        """
        circuits = [run_input] if isinstance(run_input, QuantumCircuit) else run_input

        if len(circuits) == 0:
            raise ValueError("Empty list of circuits submitted for execution.")

        # Catch old iqm-client options
        if (
            "max_circuit_duration_over_t2" in unknown_options
            or "heralding_mode" in unknown_options
        ):
            warnings.warn(
                DeprecationWarning(
                    "max_circuit_duration_over_t2 and heralding_mode are deprecated, please use "
                    + "circuit_compilation_options instead."
                )
            )
        if circuit_compilation_options is None:
            cc_options_kwargs = {}
            if "max_circuit_duration_over_t2" in unknown_options:
                cc_options_kwargs["max_circuit_duration_over_t2"] = unknown_options.pop(
                    "max_circuit_duration_over_t2"
                )
            if "heralding_mode" in unknown_options:
                cc_options_kwargs["heralding_mode"] = unknown_options.pop(
                    "heralding_mode"
                )
            circuit_compilation_options = CircuitCompilationOptions(**cc_options_kwargs)

        if unknown_options:
            warnings.warn(f"Unknown backend option(s): {unknown_options}")

        if circuit_callback:
            circuit_callback(circuits)

        circuits_serialized: CircuitBatch = [
            self.serialize_circuit(circuit, qubit_index_to_name) for circuit in circuits
        ]

        if self._use_default_calibration_set:
            default_calset_id = self.architecture.calibration_set_id
            if self._calibration_set_id != default_calset_id:
                warnings.warn(
                    f"Server default calibration set has changed from {self._calibration_set_id} "
                    f"to {default_calset_id}. Create a new IQMBackend if you wish to transpile the "
                    "circuits using the new calibration set."
                )
        try:

            def create_run_request(
                circuits: CircuitBatch,
                calibration_set_id: UUID | str,
                *,
                qubit_mapping: QubitMapping | None = None,
                shots: int = 1,
                options: CircuitCompilationOptions | None = None,
            ) -> CircuitJobDefinition:
                """Create a run request for executing circuits without sending it to the server.

                This is called in :meth:`submit_circuits` and does not need to be called separately in normal usage.

                Can be used to inspect the run request that would be submitted by :meth:`submit_circuits`, without actually
                submitting it for execution.

                Args:
                    circuits: Circuits to be executed.
                    qubit_mapping: Mapping of logical qubit names to physical qubit names.
                        Can be set to ``None`` if all ``circuits`` already use physical qubit names.
                        Note that the ``qubit_mapping`` is used for all ``circuits``.
                    calibration_set_id: ID of the calibration set to use, or ``None`` to use the current default calibration.
                    shots: Number of times ``circuits`` are executed. Must be greater than zero.
                    options: Various discrete options for compiling quantum circuits to instruction schedules.

                Returns:
                    CircuitJobDefinition that would be submitted by equivalent call to :meth:`submit_circuits`.

                """
                if shots < 1:
                    raise ValueError("Number of shots must be greater than zero.")
                if options is None:
                    options = CircuitCompilationOptions()

                for i, circuit in enumerate(circuits):
                    try:
                        if isinstance(circuit, _Circuit):
                            raise CircuitValidationError(
                                f"Circuit {i}: obsolete circuit type."
                            )
                        if isinstance(circuit, QIRCode):
                            # do not validate
                            continue
                        # validate the circuit against the static information in iqm.iqm_client.models._SUPPORTED_OPERATIONS
                        validate_circuit(circuit)
                    except ValueError as e:
                        raise CircuitValidationError(
                            f"The circuit at index {i} failed the validation: {e}"
                        ).with_traceback(e.__traceback__)

                dynamic_quantum_architecture = self.architecture

                validate_qubit_mapping(
                    dynamic_quantum_architecture, circuits, qubit_mapping
                )
                # validate the circuit against the calibration-dependent dynamic quantum architecture
                validate_circuit_instructions(
                    dynamic_quantum_architecture,
                    circuits,
                    qubit_mapping,
                    validate_moves=options.move_gate_validation,
                    must_close_sandwiches=False,
                )

                return CircuitJobDefinition(
                    qubit_mapping=qubit_mapping,
                    circuits=circuits,
                    calibration_set_id=calibration_set_id
                    if is_valid_uuid(calibration_set_id)
                    else None,
                    shots=shots,
                    max_circuit_duration_over_t2=options.max_circuit_duration_over_t2,
                    heralding_mode=options.heralding_mode,
                    move_gate_validation=options.move_gate_validation,
                    move_gate_frame_tracking=options.move_gate_frame_tracking,
                    active_reset_cycles=options.active_reset_cycles,
                    dd_mode=options.dd_mode,
                    dd_strategy=options.dd_strategy,
                )

            # Local copy of IQMClient.create_run_request (35.x) / create_circuit_job_definition (36.x),
            # validating against the cached architecture instead of calling the IQM server.
            run_request = create_run_request(
                circuits_serialized,
                calibration_set_id=self._calibration_set_id,
                shots=shots,
                options=circuit_compilation_options,
            )
        except CircuitValidationError as e:
            raise CircuitValidationError(
                f"{e}\nMake sure the circuits have been transpiled using the same backend that you used to submit "
                f"the circuits."
            ) from e

        return run_request

    def run(
        self,
        run_input: QuantumCircuit | list[QuantumCircuit],
        shots: int | None = None,
        **options,
    ):
        """Submit circuit(s) for execution through HEAppE.

        Args:
            run_input: Transpiled circuit(s) to run.
            shots: Shots per circuit; ``backend.options.shots`` if ``None``.
            **options: ``memory``, ``job_name``, ``walltime_limit_secs``, ``use_sessions``,
                ``circuit_compilation_options``, ``circuit_callback``, ``qubit_index_to_name``.

        Returns:
            QJob: The submitted job.
        """
        if shots is None:
            shots = int(self.options.shots)
        circuits = (
            [run_input] if isinstance(run_input, QuantumCircuit) else list(run_input)
        )

        run_request = self.create_circuit_job_definition(
            run_input=circuits,
            shots=shots,
            circuit_compilation_options=options.get(
                "circuit_compilation_options", CircuitCompilationOptions()
            ),
            circuit_callback=options.get("circuit_callback"),
            qubit_index_to_name=options.get("qubit_index_to_name"),
        )

        serialized_run_request = run_request.model_dump_json()

        payloads = {"run_request": serialized_run_request}

        circuit_headers = [
            {"name": c.name, "metadata": dict(c.metadata or {})} for c in circuits
        ]
        return self._submit(
            payloads, shots=shots, circuit_headers=circuit_headers, **options
        )

    def transpile(
        self, circuits: list[QuantumCircuit] | QuantumCircuit, **kwargs
    ) -> list[QuantumCircuit] | QuantumCircuit:
        """Transpile the given circuits to the IQM backend's native gate set and qubit mapping.

        Keeps each circuit's name and metadata, as ``qiskit.transpile`` does
        (``transpile_to_IQM`` drops the metadata on backends with a resonator).
        """
        if isinstance(circuits, list):
            return [self._transpile_one(circuit, **kwargs) for circuit in circuits]
        return self._transpile_one(circuits, **kwargs)

    def _transpile_one(self, circuit: QuantumCircuit, **kwargs) -> QuantumCircuit:
        """Transpile one circuit and restore its name and metadata."""
        transpiled = transpile_to_IQM(circuit=circuit, backend=self, **kwargs)
        transpiled.name = circuit.name
        transpiled.metadata = dict(circuit.metadata or {})
        return transpiled
