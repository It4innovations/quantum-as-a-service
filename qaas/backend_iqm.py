import json
import time
import os
import sys
import logging
import copy
from uuid import UUID
from iqm.qiskit_iqm import (
    IQMBackend,
    transpile_to_IQM as transpile_to_IQM_orig,
    IQMTarget
)
from iqm.iqm_client.transpile import ExistingMoveHandlingOptions

from qiskit import QuantumCircuit
from qiskit.transpiler.layout import Layout
from qiskit.qasm2 import dumps as qasm2_dumps

from py4heappe.heappe_v6.core.models import (
    EnvironmentVariableExt,
    CommandTemplateParameterValueExt
)
from .backend import QBackend,QJob
from .utils import QException
from .client import QClient
from .backend_metadata import QBackendMetadata


log = logging.getLoggerClass()(__name__, os.environ.get(
    'QPROVIDER_LOGLEVEL', 'INFO').upper())

# Formatter for consistent output
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# Decide handler: file or stderr
logfile = os.environ.get("QPROVIDER_LOGFILE")
if logfile:
    handler = logging.FileHandler(logfile, mode="a")
else:
    handler = logging.StreamHandler(sys.stderr)

handler.setFormatter(formatter)
log.addHandler(handler)


class QBackendIQM(QBackend,IQMBackend):
    """
    QaaS wrapper around IQMBackend for quantum job execution through HEAppE.

    This class extends IQMBackend to provide integration with the LEXIS platform
    via HEAppE and QClient, enabling remote quantum circuit execution on IQM
    quantum computers. The backend handles job submission, status monitoring,
    and result retrieval through the HEAppE infrastructure.

    :param client: QClient instance for HEAppE communication
    :type client: QClient
    :param kwargs: Additional arguments passed to parent IQMBackend
    :type kwargs: dict

    :raises QException: When backend initialization fails
    :raises QAuthException: When authentication with QClient fails
    :raises RuntimeError: When backend initialization via QClient fails

    :cvar DEFAULT_POLL_TIME: Default polling interval in seconds for job status checks
    :vartype DEFAULT_POLL_TIME: int

    .. note::
        The backend is initialized by submitting a backend initialization job
        through HEAppE and waiting for the IQMBackend instance to be returned.

    Example:
        >>> client = QClient(token, project, resource)
        >>> backend = QBackend(client)
        >>> job = backend.run(circuit, shots=1000)
    """

    DEFAULT_POLL_TIME = 0.5

    #pylint: disable=W0231
    def __init__(self, client: QClient, backend_metadata: QBackendMetadata, calibration_set_id:UUID=None,**kwargs):
        """
        Initialize QBackend with QClient for HEAppE communication.

        Creates a QBackend instance by submitting an initialization job through
        HEAppE to retrieve the underlying IQMBackend configuration and capabilities.
        The initialization process involves job submission, status monitoring,
        and result retrieval.

        :param client: QClient instance for communicating with HEAppE
        :type client: QClient
        :param kwargs: Additional keyword arguments passed to IQMBackend parent class
        :type kwargs: dict

        :raises QException: When client is invalid or initialization fails
        :raises QAuthException: When QClient authentication fails
        :raises RuntimeError: When backend initialization job fails or times out

        .. warning::
            The initialization process may take several minutes as it involves
            remote job submission and execution through HEAppE infrastructure.
        """
        QBackend.__init__(self, client, backend_metadata=backend_metadata,calibration_set_id=calibration_set_id, **kwargs)
        
        
        # FIXes loading of iqm_ attrs of IQMTarget
        results = self._qclient.get_job_results(
            self.init_job_id,
            [
                f"/{self.init_job_id}/{self.init_task_ids[0]}/iqm_target_attrs.pkl"
            ],
            use_dill=[False],
            job_status=self._init_job_status, task_ids=self.init_task_ids)
        log.debug("INIT - HEAppE - results: '%s'", str(results))
        
        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        iqm_attrs = results['iqm_target_attrs']
        
        for attr, value in iqm_attrs.items():
            setattr(self.target, attr, value)
            setattr(self.remote_backend.target, attr, value)

        self.name = "QBackendIQM"

        
    def _get_iqm_target_attrs(self):
        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        return {
            attr: getattr(self.target, attr)
            for attr in dir(self.target)
            if attr.startswith('iqm_') and not attr.startswith('__')
        }

    def get_iqm_backend(self) -> IQMBackend:
        """
        Extract pure IQMBackend instance from current QBackend.

        Creates a deep copy of the current instance with QClient references
        removed, returning a standalone IQMBackend that can be used independently
        of the HEAppE infrastructure.

        :returns: Pure IQMBackend instance without QClient dependencies
        :rtype: IQMBackend

        :raises QException: When deep copy operation fails
        :raises AttributeError: When required IQMBackend attributes are missing

        .. warning::
            The returned IQMBackend will not have access to HEAppE job management
            capabilities and should only be used for direct IQM operations.

        Example:
            >>> qbackend = QBackend(client)
            >>> iqm_backend = qbackend.get_iqm_backend()
            >>> # Use iqm_backend for direct IQM operations
        """

        qclient = self._qclient
        delattr(self, '_qclient')
        iqm_backend_copy = copy.deepcopy(self)
        self._qclient = qclient
        return iqm_backend_copy

    def run(self, run_input:QuantumCircuit|list[QuantumCircuit],
            # Transpilation parameters (IQM specific)
            target: IQMTarget | None = None,
            perform_move_routing: bool = True,
            optimize_single_qubits: bool = True,
            ignore_barriers: bool = False,
            remove_final_rzs: bool = True,
            existing_moves_handling: ExistingMoveHandlingOptions | None = None,
            restrict_to_qubits: list[int] | list[str] | None = None,
            circuit_compilation_options=None,
            circuit_callback=None,
            qubit_mapping=None,
            # QBackend.run arguments
            shots=1000,
            # Qiskit tranpilation parameters,
            initial_layout: Layout | dict | list | None = None,
            basis_gates = None,
            coupling_map = None,
            instruction_durations = None,
            inst_map = None,
            dt = None,
            timing_constraints = None,
            optimization_level = None,
            optimization_method = None,
            **kwargs) -> "QJob":
        """
        Execute quantum circuit(s) by submitting HEAppE job via QClient.

        Submits quantum circuits for execution on IQM hardware through HEAppE
        infrastructure. Handles both single circuits and lists of circuits,
        serializing them to QASM3 format for remote execution.

        :param target: An alternative target to compile to than the backend, using 
            this option requires intimate knowledge
            of the transpiler and thus it is not recommended to use.
        :param perform_move_routing: Whether to perform MOVE gate routing.
        :param optimize_single_qubits: Whether to optimize single qubit gates away.
        :param ignore_barriers: Whether to ignore barriers when optimizing single qubit gates away.
        :param remove_final_rzs: Whether to remove the final z rotations. It is recommended always
            to set this to true as the final RZ gates do no change 
            the measurement outcomes of the circuit.
        :param existing_moves_handling: How to handle existing MOVE gates in the circuit,
            required if the circuit contains MOVE gates.
        :param restrict_to_qubits: Restrict the transpilation to only use these specific
                physical qubits. Note that you will have to pass this 
                information to the ``backend.run`` method as well as a dictionary.
        :param circuit_compilation_options: IQM-specific compilation options
        :type circuit_compilation_options: dict, optional
        :param circuit_callback: Callback function for circuit processing
        :type circuit_callback: callable, optional  
        :param qubit_mapping: Custom qubit mapping for circuit execution
        :type qubit_mapping: dict, optional
        
        
        :param run_input: Quantum circuit(s) to execute
        :type run_input: QuantumCircuit or List[QuantumCircuit]
        :param shots: Number of measurement shots to perform
        :type shots: int
        :param initial_layout: The initial layout to use for the transpilation,
            same as :func:`~qiskit.compiler.transpile`.
        :param basis_gates: :func:`~qiskit.compiler.transpile`
        :param coupling_map: :func:`~qiskit.compiler.transpile`
        :param instruction_durations: :func:`~qiskit.compiler.transpile`
        :param inst_map: :func:`~qiskit.compiler.transpile`
        :param dt: :func:`~qiskit.compiler.transpile`
        :param timing_constraints: :func:`~qiskit.compiler.transpile`
        :param optimization_level: :func:`~qiskit.compiler.transpile`
        :param optimization_method: :func:`~qiskit.compiler.transpile`
        :param kwargs: Additional runtime parameters including:

            * walltime_limit (int): Maximum job execution time in seconds (default: 3600)
            * template_id (str): HEAppE template identifier
            * min_cores (int): Minimum CPU cores required
            * max_cores (int): Maximum CPU cores allowed

        :type kwargs: dict

        :returns: QJob instance for monitoring execution and retrieving results
        :rtype: QJob

        :raises QException: When job submission fails or parameters are invalid
        :raises QAuthException: When authentication fails during job submission
        :raises ValueError: When circuit serialization to QASM3 fails
        :raises TypeError: When run_input contains invalid circuit types

        .. note::
            Circuits are serialized to QASM3 format for transmission to HEAppE.
            Complex compilation options and callbacks are pickle-serialized and
            base64-encoded for remote execution.

        Example:
            >>> circuit = QuantumCircuit(2, 2)
            >>> circuit.h(0)
            >>> circuit.cx(0, 1)  
            >>> circuit.measure_all()
            >>> job = backend.run(circuit, shots=1000, walltime_limit=1800)
        """

        _, heappe_job_id = QBackend.run(self,
                     run_input,
                     shots=shots,
                     transpilation_options={
                        'target': target,
                        'perform_move_routing': perform_move_routing,
                        'optimize_single_qubits': optimize_single_qubits,
                        'ignore_barriers': ignore_barriers,
                        'remove_final_rzs': remove_final_rzs,
                        'existing_moves_handling': existing_moves_handling,
                        'restrict_to_qubits': restrict_to_qubits,
                        'circuit_compilation_options': circuit_compilation_options,
                        'circuit_callback': circuit_callback,
                        'qubit_mapping': qubit_mapping,
                        'calibration_id': self._calibration_set_id
                     },
                    initial_layout=initial_layout,
                    basis_gates=basis_gates,
                    coupling_map=coupling_map,
                    instruction_durations=instruction_durations,
                    inst_map=inst_map,
                    dt=dt,
                    timing_constraints=timing_constraints,
                    optimization_level=optimization_level,
                    optimization_method=optimization_method,
                    run_options=kwargs
                )
        return QJob(self,heappe_job_id)

    def retrieve_job(self, job_id: str) -> 'QJob':
        """
        Retrieve existing QJob by HEAppE job identifier.

        Creates a QJob instance for an existing HEAppE job, allowing status
        monitoring and result retrieval for previously submitted quantum jobs.

        :param job_id: HEAppE job identifier string
        :type job_id: str

        :returns: QJob instance for the specified job
        :rtype: QJob

        :raises QException: When job_id is invalid or job retrieval fails
        :raises QAuthException: When authentication fails during job access
        :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
        :raises ValueError: When job_id format is invalid

        .. note::
            This method does not verify job existence or status - it simply
            creates a QJob wrapper for the provided job_id.

        Example:
            >>> job_id = "heappe_12345"
            >>> existing_job = backend.retrieve_job(job_id)
            >>> status = existing_job.status()
            >>> if status == 'DONE':
            ...     result = existing_job.result()
        """

        return QJob(self, job_id)

    def update_from_remotebackend(self, remote_backend_instance: IQMBackend):
        """
        Update this QBackend with attributes from an IQMBackend instance.

        Copies all attributes from the provided IQMBackend instance to this
        QBackend, effectively merging IQM backend capabilities with HEAppE
        integration. This method is used internally during initialization.

        :param remote_backend_instance: IQMBackend instance to copy attributes from
        :type remote_backend_instance: IQMBackend

        :returns: Self reference for method chaining
        :rtype: QBackend

        :raises QException: When attribute copying fails
        :raises QAuthException: When authentication fails (if qclient operations involved)
        :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
        :raises TypeError: When iqm_backend_instance is not a valid IQMBackend
        :raises AttributeError: When required IQMBackend attributes are missing

        .. warning::
            This method performs attribute copying that may overwrite existing
            QBackend-specific attributes. The _qclient attribute is preserved.

        Example:
            >>> # Internal usage during backend initialization
            >>> iqm_backend = get_iqm_backend_from_heappe()
            >>> qbackend.update_from_iqmbackend(iqm_backend)
        """

        return QBackend.update_from_remotebackend(self, remote_backend_instance)

    def transpile(self, circuit: QuantumCircuit, **kwargs) -> QuantumCircuit:  # pylint: disable=c0103
        """Customized transpilation to IQM backends.

        Works with both the Crystal and Star architectures.

        :param circuit: The circuit to be transpiled without MOVE gates.
        :param backend: QBackend instance
        :param remote: If True, run transpilation on a remote cluster; 
                otherwise, run locally on your machine. Defaults to False.




        :param initial_layout: The initial layout to use for the transpilation, 
        same as :func:`~qiskit.compiler.transpile`.
        :param perform_move_routing: Whether to perform MOVE gate routing.
        :param optimize_single_qubits: Whether to optimize single qubit gates away.
        :param ignore_barriers: Whether to ignore barriers when optimizing single qubit gates away.
        :param remove_final_rzs: Whether to remove the final z rotations.
                It is recommended always to set this to true as
                the final RZ gates do no change the measurement outcomes of the circuit.
        :param existing_moves_handling: How to handle existing MOVE gates in the circuit,
                required if the circuit contains
                MOVE gates.
        :param restrict_to_qubits: Restrict the transpilation to only use these specific
                physical qubits. Note that you will have to pass this 
                information to the ``backend.run`` method as well as a dictionary.
        :param qiskit_transpiler_kwargs: Arguments to be passed to the Qiskit transpiler.



        :return: Transpiled instance of "QuantumCircuit" ready for running on the backend.

        :raises QException: If a general transpilation error occurs.
        :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
        :raises QAuthException: If authentication with the remote cluster fails.
        """
        # Transpile localy
        return transpile_to_IQM_orig(circuit=circuit, backend=self.remote_backend, **kwargs)


def transpile_to_IQM(circuit: QuantumCircuit, backend: QBackend,  # pylint: disable=c0103
                     **kwargs) -> QuantumCircuit:
    """Customized transpilation to IQM backends.

    Works with both the Crystal and Star architectures.

    :param circuit: The circuit to be transpiled without MOVE gates.
    :param backend: QBackend instance




    :param initial_layout: The initial layout to use for the transpilation,
        same as :func:`~qiskit.compiler.transpile`.
    :param perform_move_routing: Whether to perform MOVE gate routing.
    :param optimize_single_qubits: Whether to optimize single qubit gates away.
    :param ignore_barriers: Whether to ignore barriers when optimizing single qubit gates away.
    :param remove_final_rzs: Whether to remove the final z rotations.
        It is recommended always to set this to true as
        the final RZ gates do no change the measurement outcomes of the circuit.
    :param existing_moves_handling: How to handle existing MOVE gates in the circuit,
        required if the circuit contains MOVE gates.
    :param restrict_to_qubits: Restrict the transpilation to only use these specific 
            physical qubits. Note that you will have to pass this 
            information to the ``backend.run`` method as well as a dictionary.
    :param qiskit_transpiler_kwargs: Arguments to be passed to the Qiskit transpiler.



    :return: Transpiled instance of "QuantumCircuit" ready for running on the backend.

    :raises QException: If a general transpilation error occurs.
    :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
    :raises QAuthException: If authentication with the remote cluster fails.
    """
    return backend.transpile(circuit=circuit, **kwargs)

