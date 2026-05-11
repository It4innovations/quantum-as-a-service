""" QaaS Backend Core

:raises RuntimeError: _description_
:raises NotImplementedError: _description_
:raises NotImplementedError: _description_
:raises TimeoutError: _description_
:raises QException: _description_
:raises QException: _description_
"""
from abc import abstractmethod
from uuid import UUID
import time
import os
import sys
import logging

from qiskit import QuantumCircuit
from qiskit.result import Result as QiskitResult
from iqm.qiskit_iqm import IQMJob

from qiskit.qasm3 import dumps as qasm3dumps

from iqm.station_control.interface.models import CircuitMeasurementResultsBatch

from py4heappe.heappe_v6.core.models import (
    EnvironmentVariableExt
)

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


class QBackend():
    """
    QaaS wrapper around Quantum providers for quantum job execution through HEAppE.

    :param client: QClient instance for HEAppE communication
    :type client: QClient
    :param kwargs: Additional arguments passed to parent IQMBackend
    :type kwargs: dict

    :raises QException: When backend initialization fails
    :raises QAuthException: When authentication with QClient fails
    :raises RuntimeError: When backend initialization via QClient fails

    .. note::
        The backend is initialized by submitting a backend initialization job
        through HEAppE and waiting for the IQMBackend instance to be returned.

    Example:
        >>> client = QClient(token, project, resource)
        >>> backend = QBackend(client)
        >>> job = backend.run(circuit, shots=1000)
    """

    def __init__(self, client: QClient, backend_name:str=None, backend_metadata:QBackendMetadata=None, calibration_set_id:UUID=None,**kwargs):
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
        self._qclient = client
        
        self.backend_name = backend_name or backend_metadata.backend_name
        self.qbackend_metadata = backend_metadata
        self._calibration_set_id = calibration_set_id

        # Get IQM backend using QClient to submit initialization job
        backend_from_heappe = self._get_backend_from_heappe()
        self.update_from_remotebackend(backend_from_heappe)


    def _get_backend_from_heappe(self):
        """
        Initialize backend by submitting HEAppE job through QClient.

        Internal method that handles the backend initialization process by
        submitting a specialized HEAppE job with the 'init_backend' command.
        Polls job status until completion and retrieves the IQMBackend instance.

        :returns: Initialized IQMBackend instance from HEAppE execution
        :rtype: IQMBackend

        :raises QException: When job submission or status retrieval fails
        :raises QAuthException: When authentication fails during initialization
        :raises RuntimeError: When initialization job fails or returns invalid results
        :raises TimeoutError: When initialization job exceeds walltime limit

        .. note::
            This method creates a temporary HEAppE job with a 30-second walltime
            limit specifically for backend initialization.
        """

        # Submit backend initialization job using QClient

        init_job_data = {
            'name': 'backend_init_job',
            'walltime_limit': 30,
            'tasks': [{'template_parameter_values': []}],
            'environment_variables': [
                EnvironmentVariableExt(name="Q_COMMAND",
                                    value="backend_init"
                                    )
            ]
        }
        q_arg_value = ""
        if self.backend_name:
            q_arg_value = self.backend_name
        if self._calibration_set_id:
            q_arg_value+=","+self._calibration_set_id
        
        if q_arg_value != "":
            init_job_data['environment_variables'].append(
                EnvironmentVariableExt(name="Q_OPTIONAL_ARG",
                                    value=q_arg_value
                                    )
            )

        self.init_job_id = self._qclient.submit_quantum_job(init_job_data)

        # Wait for initialization to complete using QClient
        self._init_job_status, _, self.init_task_ids = self._qclient.get_job_status(self.init_job_id)
        while self._init_job_status not in ["FINISHED", "FAILED"]:
            time.sleep(QClient.DEFAULT_POLL_TIME)
            self._init_job_status, _, _ = self._qclient.get_job_status(self.init_job_id)

        results = self._qclient.get_job_results(
            self.init_job_id, [f"/{self.init_job_id}/{self.init_task_ids[0]}/backend.pkl"],
            use_dill=[False],
            job_status=self._init_job_status, task_ids=self.init_task_ids)
        log.debug("INIT - HEAppE - results: '%s'", str(results))
        if results and 'backend' in results:
            self.remote_backend = results['backend']
            return results['backend']
        else:
            raise RuntimeError("Failed to initialize backend via QClient")

    def run(self, run_input: QuantumCircuit | list[QuantumCircuit] | str | list[str],
            shots=1000,
            # everything else is run parameter
            **run_options)->tuple["QJob",str]:
        """
        Execute quantum circuit(s) by submitting HEAppE job via QClient.

        Submits quantum circuits for execution on provider's hardware through HEAppE
        infrastructure. Handles both single circuits and lists of circuits.

        :param run_input: Quantum circuit(s) to execute
        :type run_input: QuantumCircuit or List[QuantumCircuit] or str or List[str] (OpenQASM)
        :param shots: Number of measurement shots to perform
        :type shots: int
        
        :param run_options: Additional runtime parameters including:

            * walltime_limit (int): Maximum job execution time in seconds (default: 3600)

        :type run_options: dict

        :returns: A tuple where:
          - First element (QJob): Instance for monitoring execution and retrieving results
          - Second element (str): String identifier
        :rtype: tuple[QJob, str]


        :raises QException: When job submission fails or parameters are invalid
        :raises QAuthException: When authentication fails during job submission
        :raises TypeError: When run_input contains invalid circuit types


        Example:
            >>> circuit = QuantumCircuit(2, 2)
            >>> circuit.h(0)
            >>> circuit.cx(0, 1)  
            >>> circuit.measure_all()
            >>> job = backend.run(circuit, shots=1000, walltime_limit=1800)
        """

        # Handle both single circuit and list of circuits
        run_circuits = run_input if isinstance(run_input, list) else [run_input]
        # All should be OpenQASM
        run_circuits_qasm = []
        for c in run_circuits:
            if isinstance(c, QuantumCircuit):
                if self.backend_name == "VLQ":
                    # We must give 'move' a definition so the exporter accepts it.
                    # We use an 'opaque' definition (empty circuit) to keep it as a single block.
                    for instr in c.data:
                        if instr.operation.name == 'move':
                            if not hasattr(instr.operation, 'definition') or instr.operation.definition is None:
                                # IQM 'move' usually involves 2 qubits (or a qubit and a resonator)
                                dummy_circ = QuantumCircuit(instr.operation.num_qubits)
                                instr.operation.definition = dummy_circ
                # Export to OpenQASM3 with mapping aware transpilation
                run_circuits_qasm.append(qasm3dumps(c))
            else:
                run_circuits_qasm.append(c)
        
        # Prepare RUN_KWARGS environment variable
        run_kwargs = {
            'shots': shots,
            # Pass through other runtime parameters
            **{
                k: v for k, v in run_options.items()
                if k not in ['walltime_limit', 'template_id', 'min_cores', 'max_cores']
            }
        }

        # NOTE: currently are 'template_id', 'min_cores', 'max_cores' unused

        log.debug("run_kwargs: %s", str(run_kwargs))
        log.debug("len(circuit): %d", len(run_circuits_qasm))
        # log.debug("Circuit IS string: %s", "yes" if isinstance(run_circuits_qasm[0], str) else "no")

        # Prepare job data for HEAppE submission

        job_name_core = getattr(run_input, "name", "circuits") \
            if not isinstance(run_input, list) \
            else "multiple_circuits"

        job_data = {
            'name': f'quantum_run_{job_name_core}',
            'walltime_limit': run_options.get('walltime_limit', 7200), # 2 hours
            'min_cores': 1, #NOTE: currently unused
            'max_cores': 1, #NOTE: currently unused
            'tasks': [{'template_parameter_values': []}],
            # Set environment variables for the job
            'environment_variables': [
                EnvironmentVariableExt(name="Q_COMMAND", value="backend_run")
            ]
        }
        if self._qclient.provider_token:
            token_var_name = "Q_TOKEN"
            if self.qbackend_metadata.software_stack() == "IQM":
                token_var_name = "IQM_TOKEN"
            job_data['environment_variables'] = EnvironmentVariableExt(name=token_var_name, value=self._qclient.provider_token)

        # Submit job using QClient
        heappe_job_id = self._qclient.submit_quantum_job(job_data,
                                                         backend=self.remote_backend,
                                                         circuits=run_circuits_qasm,
                                                         run_options=run_kwargs)
        return [self, heappe_job_id]

    def retrieve_job(self, job_id: str) -> 'QJob':
        """Retrieve HEAppE job wrapper

        :param job_id: 
        :raises NotImplementedError: Not Implemented
        :return: QJob
        """
        raise NotImplementedError()

    def update_from_remotebackend(self, remote_backend_instance):
        """
        Update this QBackend with attributes from an IQMBackend instance.

        Copies all attributes from the provided IQMBackend instance to this
        QBackend, effectively merging IQM backend capabilities with HEAppE
        integration. This method is used internally during initialization.

        :param remote_backend_instance: IQMBackend instance 
            to copy attributes from
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
            >>> qbackend.update_from_remotebackend(iqm_backend)
        """

        for key, value in remote_backend_instance.__dict__.items():
            setattr(self, key, value)
        self.remote_backend = remote_backend_instance
        return self

    @abstractmethod
    def transpile(self, circuit: QuantumCircuit, **kwargs) -> QuantumCircuit:  # pylint: disable=c0103
        """Customized transpilation to Quantum backends."""
        raise NotImplementedError()


def transpile(circuit: QuantumCircuit, backend: QBackend,  # pylint: disable=c0103
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
        physical qubits. Note that you will have to pass this information
        to the ``backend.run`` method as well as a dictionary.
    :param qiskit_transpiler_kwargs: Arguments to be passed to the Qiskit transpiler.



    :return: Transpiled instance of "QuantumCircuit" ready for running on the backend.

    :raises QException: If a general transpilation error occurs.
    :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
    :raises QAuthException: If authentication with the remote cluster fails.
    """
    return backend.transpile(circuit=circuit, **kwargs)


class QJob():
    """
    QaaS wrapper around QJob for managing quantum job execution through HEAppE.

    This class QJob provides integration with the LEXIS platform via HEAppE,
    enabling remote quantum job submission and result retrieval 
    from several provider's quantum computers.
    The job lifecycle involves HEAppE job management and eventual QJob result processing.

    :param backend: The quantum backend instance
    :type backend: QBackend
    :param heappe_job_id: HEAppE job identifier for tracking remote execution
    :type heappe_job_id: int

    ## Exceptions:
    :raises QException: When job initialization fails
    :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
    :raises QAuthException: When authentication with backend fails

    .. note::
        The job is initially created with a placeholder job_id and later updated
        with the actual QJob instance when results become available.

    Example:
        >>> backend = provider.get_backend()
        >>> job = QJob(backend, 9024)
        >>> result = job.result()
    """

    def __init__(self, backend: QBackend, heappe_job_id: int, job_type="circuit"):
        """
        Initialize QJob with backend and HEAppE job identifier.

        Creates a QJob instance that wraps HEAppE job management with QJob
        functionality. The job starts with a placeholder ID and is later updated
        with the actual QJob when execution completes.

        :param backend: The quantum backend instance for job execution
        :type backend: QBackend
        :param heappe_job_id: HEAppE job identifier for remote tracking
        :type heappe_job_id: str
        :param kwargs: Additional keyword arguments passed to QJob parent class
        :type kwargs: dict

        :raises QException: When backend is invalid or job initialization fails
        :raises QAuthException: When backend authentication is unsuccessful
        """

        self._backend = backend
        self.job_id = heappe_job_id
        self._qclient = backend._qclient
        self._qaas_run_started = time.time()

        self._transpiled_circuits: QuantumCircuit | list[QuantumCircuit] = None
        
        self._result = None
        self._type = job_type

        ###################
        # Client runtimes #
        ###################
        
        
        # Run without data transfer
        self.qaas_runtime = -1.0
        # Output data fetched from HEAppE
        self.qaas_fetching_runtime = -1.0
        # Update of class instances attributes
        self.qaas_instance_update_runtime = -1.0
        
        ###################
        # Remote runtimes #
        ###################
        # Initialization of pickles
        self.remote_initialization_runtime = -1.0
        # Optional transpilation time (if set)
        self.remote_backend_run_transpilation_runtime = -1.0
        # Runtime of provider's .run() method and results fetching
        self.iqm_client_job_runtime = -1.0
        # Total QaaS backend_run runtime on remote machine (initialization + transpilation + .run() + .results() + postprocessing)
        self.remote_backend_runtime = -1.0
        # Fetch of results from provider runtime
        self.remote_iqm_client_results_fetching_runtime = -1.0
        # Postprocessing runtime
        self.remote_backend_run_postprocessing_runtime = -1.0
        # Real execution time on quantum machine HW
        self.remote_hw_runtime = -1.0
        
        # Events timeline of QaaS
        self.events = {
            'client_run_started': None,
            'backend_run_initialization_started': None,
            'backend_run_initialization_ended': None,
            'backend_run_transpilation_started': None,
            'backend_run_transpilation_ended': None,
            'iqm_client_run_started': None,
            'iqm_client_run_ended': None,
            'iqm_client_results_fetching_started': None,
            'iqm_client_run_results_fetching_ended': None,
            'backend_run_postprocessing_started': None,
            'backend_run_postprocessing_ended': None,
            'client_run_ended': None,
            'client_fetch_data_started': None,
            'client_fetch_data_ended': None,
            'client_instance_update_started': None,
            'client_instance_update_ended': None,
        }

        self.remote_job:IQMJob = None  # Will be set when results are available

    def result(self, timeout_secs: float = 600, cancel_after_timeout: bool = False) -> QiskitResult | CircuitMeasurementResultsBatch:  # pylint: disable=W0221
        """
        Retrieve quantum job results from HEAppE execution.

        Polls HEAppE job status until completion, then retrieves and processes
        the results. If an QJob instance is available in the results, it
        updates this QJob instance and returns the QJob results. Otherwise,
        returns raw HEAppE results.

        :param timeout_secs: Maximum time in seconds to wait for job completion
        :type timeout_secs: float
        :param cancel_after_timeout: Whether to cancel job after timeout expires
        :type cancel_after_timeout: bool

        :returns: Job execution results, either from QJob or raw HEAppE results
        :rtype: QiskitResult or dict

        :raises QException: When job execution fails or results are unavailable
        :raises QAuthException: When authentication fails during result retrieval
        :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job
        :raises TimeoutError: When timeout is exceeded


        Example:
            >>> job = backend.run(circuit, shots=1000)
            >>> result = job.result(timeout=600)
            >>> counts = result.get_counts()
        """
        
        if self._result:
            return self._result

        # Fetching of results started
        timeout_start = time.time()

        job_status, _, task_ids = self._qclient.get_job_status(
            self.job_id)

        while job_status not in ["FINISHED", "FAILED"]:
            time.sleep(QClient.DEFAULT_POLL_TIME)
            job_status, _, _ = self._qclient.get_job_status(
                self.job_id)
            if timeout_secs > 0.0 and time.time()-timeout_start > timeout_secs:
                if cancel_after_timeout and not self._backend.cancel_job(self.job_id):
                    raise QException(
                        f"Unable to cancel job with id:{self.job_id}")
                if cancel_after_timeout:
                    raise TimeoutError(f"Job was cancelled after {timeout_secs}s")
                raise TimeoutError(f"Job timeouted after {timeout_secs}s")

        run_ended = time.time()
        self.qaas_runtime = run_ended - self._qaas_run_started
        # Results fetched
        log.debug("job finished in: %f s", self.qaas_runtime)
        
        results_fetching_started = time.time()
        # Get results from HEAppE job via QClient
        
        circuit_job_files = [
                f"/{self.job_id}/{task_ids[0]}/backend.pkl",
                f"/{self.job_id}/{task_ids[0]}/job.pkl",
                f"/{self.job_id}/{task_ids[0]}/results.pkl",
                f"/{self.job_id}/{task_ids[0]}/transpiled_circuits.pkl"
            ]
        circuit_job_use_dill = [False, False, True, True]
        pulla_job_files = [
                f"/{self.job_id}/{task_ids[0]}/job.pkl",
                f"/{self.job_id}/{task_ids[0]}/results.pkl",
            ]
        pulla_use_dill = [True, True]

        
        heappe_results = self._qclient.get_job_results(self.job_id,
            circuit_job_files if self._type == "circuit" else pulla_job_files,
            use_dill=circuit_job_use_dill if self._type == "circuit" else pulla_use_dill,
            job_status=job_status,task_ids=task_ids
            
        )

        if heappe_results is None:
            run_ended = None
            self.qaas_runtime = -1.0
            raise QException(f"Job {self.job_id} not finished yet")

        results_fetching_ended = time.time()
        self.qaas_fetching_runtime = results_fetching_ended - results_fetching_started
        
        log.debug("job files fetched in: %f s", self.qaas_fetching_runtime)
        
        
        # Extract the QJob from results and update this instance
        if 'job' in heappe_results and heappe_results['job']:
            
            instance_update_started = time.time()
            self.update_from_remotejob(heappe_results['job'])
            if self._type == "circuit":
                self._transpiled_circuits = heappe_results.get(
                    'transpiled_circuits')
            instance_update_ended = time.time()

            self.qaas_instance_update_runtime = instance_update_ended - instance_update_started
            self.events['client_instance_update_started'] = instance_update_started
            self.events['client_instance_update_ended'] = instance_update_ended
            
            self.events['client_run_started'] = self._qaas_run_started
            self.events['client_run_ended'] = run_ended
            self.events['client_fetch_data_started'] = results_fetching_started
            self.events['client_fetch_data_ended'] = results_fetching_ended

            log.debug("Qprovider instances updated (IQMjob) in: %f s",
                      self.qaas_instance_update_runtime)

            if isinstance(self.remote_job,IQMJob):
                if self.status() == "ERROR":
                    log.error(
                        "IQMJob failed! Error(s):\n%s",
                        self.remote_job._errors,
                    )
                if hasattr(self.remote_job, 'data') and self.remote_job.data.messages:
                    log.debug("Job messages:\n%s", "\n".join(f"  {msg.source}: {msg.message}" for msg in self.remote_job.data.messages))
            
            if self._type == "circuit":
                self._result = self.remote_job.result()
            else: # pulla
                self._result = heappe_results.get('results')
            return self._result

        self.events['client_run_started'] = self._qaas_run_started
        self.events['client_run_ended'] = run_ended
        self.events['client_fetch_data_started'] = results_fetching_started
        self.events['client_fetch_data_ended'] = results_fetching_ended
        
        # If no QJob available, return raw results. NOTE: currently unused
        self._result = heappe_results.get('results')
        return self._result

    def status(self) -> str:
        """
        Get the current status of the quantum job.

        Returns job status by checking the underlying QJob if available,
        otherwise queries HEAppE job status and maps it to Qiskit status format.

        :returns: Current job status: DONE | RUNNING | ERROR
        :rtype: str

        :raises QException: When status retrieval fails
        :raises QAuthException: When authentication fails during status check

        .. note::
            The status mapping from HEAppE to Qiskit format:

            * 'FINISHED' -> 'DONE'
            * 'WAITING' -> 'RUNNING'  
            * 'FAILED' -> 'ERROR'
            * 'UNKNOWN' -> 'ERROR'

        Example:
            >>> job = backend.run(circuit, shots=1000)
            >>> while job.status() != 'DONE':
            ...     time.sleep(10)
            >>> result = job.result()
        """
        if self.remote_job:
            return self.remote_job.status()

        heappe_status, _, _ = self._qclient.get_job_status(self.job_id)

        # Map HEAppE status to Qiskit status
        status_mapping = {
            'FINISHED': 'DONE',
            'WAITING': 'RUNNING',
            'FAILED': 'ERROR',
            'UNKNOWN': 'ERROR'
        }
        return status_mapping.get(heappe_status, 'ERROR')

    def wait_for_completion(self, timeout_secs:float=600, cancel_after_timeout=True)->bool:
        """Waits until job results are ready or job fails.

        :param timeout_secs: When less then 0.0, timeout is disabled, defaults to 600
        :param cancel_after_timeout: cancels job, when timed out, defaults to True
        :raises TimeoutError: Job run out of timeout
        :raises QException: General exception from QaaS
        :return: _description_
        """
        # Fetching of results started
        timeout_start = time.time()

        job_status, _, task_ids = self._qclient.get_job_status(
            self.job_id)

        while job_status not in ["FINISHED", "FAILED"]:
            time.sleep(QClient.DEFAULT_POLL_TIME)
            job_status, _, _ = self._qclient.get_job_status(
                self.job_id)
            if timeout_secs > 0.0 and time.time()-timeout_start > timeout_secs:
                if cancel_after_timeout and not self._backend.cancel_job(self.job_id):
                    raise QException(
                        f"Unable to cancel job with id:{self.job_id}")
                raise TimeoutError(f"Job was cancelled after {timeout_secs}s")
        return True

    def cancel_heappe_job(self, heappe_job_id: int) -> bool:
        """See doc QClient:cancel_job
        """
        return self._qclient.cancel_job(heappe_job_id)

    def update_from_remotejob(self, remote_job_instance):
        """
        Update this QJob instance with attributes from an QJob.

        Copies all attributes from the provided QJob instance to this QJob,
        effectively transforming this wrapper into a full QJob with HEAppE
        integration. This method is called internally when HEAppE results
        contain a completed QJob.

        :param remote_job_instance: The QJob instance to copy attributes from
        :type remote_job_instance: QJob

        :returns: Self reference for method chaining
        :rtype: QJob

        :raises QException: When attribute copying fails
        :raises TypeError: When iqm_job_instance is not a valid QJob

        .. warning::
            This method performs a shallow copy of all attributes from the
            QJob instance. Complex objects may still reference the original
            QJob's data structures.
        """

        for key, value in remote_job_instance.__dict__.items():
            setattr(self, key, value)
        self.remote_job = remote_job_instance
        return self

    def get_transpiled_circuits(self):
        """Getter of transpiled Quantum circuits used to run a Job
        """
        return self._transpiled_circuits
