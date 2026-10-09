import warnings
from collections.abc import Callable
from typing import Any
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
    QIRCode,
    QubitMapping,
    RunRequest,
)

# Obsolete Circuit type
from iqm.station_control.interface.models.circuit import _Circuit
from qiskit import QuantumCircuit
from qiskit.primitives import PrimitiveResult, SamplerPubResult
from qiskit.primitives.containers import BitArray, DataBin
from qiskit.result import Result
from qiskit.result.models import ExperimentResult, ExperimentResultData

from qheappe_client.backend_metadata import QBackendMetadata
from qheappe_client.qclient import QClient
from qheappe_client.qiskit.qbackend import QBackend
from qheappe_client.utils import is_valid_uuid

__all__ = ["QBackendIQM", "transpile_to_IQM"]


class QBackendIQM(QBackend, IQMBackend):
    """QaaS backend for IQM hardware. Target/qubit mapping come from IQMBackendBase,
    execution goes through QClient/HEAppE."""

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

        if use_metrics:
            metrics = self._qclient.get_machine_metrics()
            metrics = ObservationFinder(metrics)
        else:
            metrics = None

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
    def max_circuits(self) -> int | None:
        return None

    def result_to_qiskit(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
        key_order_left_to_right: bool = True,
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
            key_order_left_to_right: If True, measurement_keys[0] corresponds to clbit 0.

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

        order = "little" if key_order_left_to_right else "big"
        pubs = []
        for i, item in enumerate(raw):
            keys = item["measurement_keys"]

            if measurements_raw is not None:
                m = measurements_raw[i]
                cols = [
                    np.asarray(m[k], dtype=bool).reshape(len(m[k]), -1) for k in keys
                ]
                if len({c.shape[0] for c in cols}) != 1:
                    raise ValueError(f"PUB {i}: keys have different shot counts")
                ba = BitArray.from_bool_array(np.concatenate(cols, axis=1), order=order)

                if "counts" in item and ba.num_shots != sum(item["counts"].values()):
                    raise ValueError(f"PUB {i}: shots in measurements != sum of counts")
            else:
                n = len(next(iter(item["counts"])))
                int_counts = {
                    int(bs[::-1] if key_order_left_to_right else bs, 2): c
                    for bs, c in item["counts"].items()
                }
                ba = BitArray.from_counts(int_counts, num_bits=n)

            data = DataBin(**{reg_name: ba}, shape=ba.shape)
            pubs.append(SamplerPubResult(data, metadata={"measurement_keys": keys}))
        return PrimitiveResult(pubs, metadata={})

    def result_to_qiskit_legacy(
        self,
        raw: list[dict],
        measurements_raw: list[dict] | None = None,
        reg_name: str = "meas",
        key_order_left_to_right: bool = True,
        backend_name: str = "my_backend",
        job_id: str = "",
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
            key_order_left_to_right: If True, measurement_keys[0] corresponds to clbit 0.
            backend_name: The name of the backend used for the execution.
            job_id: The ID of the job that produced the results.

        Returns:
            A Result object containing the processed measurement results.

        Raises:
            ValueError: If the number of raw results does not match the number of measurement sets.
        """

        def to_int(bits: str) -> int:
            """
            Convert a bit string to an integer.

            Args:
                bits: A string of bits.

            Returns:
                An integer representation of the bit string, with clbit 0 as the least significant bit if key_order_left_to_right is True.
            """
            return int(bits[::-1] if key_order_left_to_right else bits, 2)

        exps = []
        for i, item in enumerate(raw):
            keys = item["measurement_keys"]
            counts = {hex(to_int(bs)): c for bs, c in item["counts"].items()}
            n = len(next(iter(item["counts"])))

            memory = None
            if measurements_raw is not None:
                m = measurements_raw[i]
                per_key = [m[k] for k in keys]  # [key][shot][bit]
                shots = len(per_key[0])
                bitstrs = [
                    "".join(str(int(b)) for k in per_key for b in k[s])
                    for s in range(shots)
                ]
                memory = [hex(to_int(s)) for s in bitstrs]

            exps.append(
                ExperimentResult(
                    shots=sum(item["counts"].values()),
                    success=True,
                    data=ExperimentResultData(counts=counts, memory=memory),
                    header={
                        "name": f"circuit-{i}",
                        "creg_sizes": [[reg_name, n]],
                        "memory_slots": n,
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
    ) -> RunRequest:
        """Create a run request without submitting it for execution.

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
            The created run request object

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
            ) -> RunRequest:
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
                    RunRequest that would be submitted by equivalent call to :meth:`submit_circuits`.

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

                return RunRequest(
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

            # Run local create_run_request fn derived from IQMClient.create_run_request
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
        self, run_input: QuantumCircuit | list[QuantumCircuit], shots: int, **options
    ):
        circuits = (
            [run_input] if isinstance(run_input, QuantumCircuit) else list(run_input)
        )

        run_request = self.create_run_request(
            run_input=circuits,
            shots=shots,
            circuit_compilation_options=options.get(
                "circuit_compilation_options", CircuitCompilationOptions()
            ),
            circuit_callback=options.get("circuit_callback"),
            qubit_index_to_name=options.get("qubit_index_to_name"),
        )

        serialized_run_request = RunRequest.model_dump_json(run_request)

        payloads = {"run_request": serialized_run_request}

        return self._submit(payloads, shots=shots, **options)

    def transpile(
        self, circuits: list[QuantumCircuit] | QuantumCircuit, **kwargs
    ) -> list[QuantumCircuit] | QuantumCircuit:
        """Transpile the given circuits to the IQM backend's native gate set and qubit mapping."""
        if isinstance(circuits, list):
            return [
                transpile_to_IQM(circuit=circuit, backend=self, **kwargs)
                for circuit in circuits
            ]
        return transpile_to_IQM(circuit=circuits, backend=self, **kwargs)
