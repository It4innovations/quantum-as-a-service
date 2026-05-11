#!/usr/bin/env python3

import os
import sys
import pickle
import time
import socket
from copy import deepcopy
from pathlib import Path
from datetime import timezone, datetime
from uuid import UUID
import dill
import jwt
from cachetools import TTLCache
from qiskit import QuantumCircuit
from qiskit.qasm3 import load as qasm3load
from iqm.qiskit_iqm import IQMBackend, IQMProvider
from iqm.qiskit_iqm.iqm_job import IQMJob
from iqm.iqm_client import JobStatus as IQMJobStatus

# from iqm.iqm_server_client.models import TimelineEntry
from iqm.pulla.pulla import SweepJob, Pulla
from iqm.iqm_client import IQMClient

from qaas.iqm_backend.backend_env_variables import (
    QAAS_ALLOWED_CLIENT_COUNT,
)
from qaas.iqm_backend.backend_service_accounting_info import AccountingInfo
from qaas.iqm_backend.backend_service_consumption import (
    initializeKafkaProducer,
    fetch_current_resource_consumption,
    record_consumption_usage,
)

print("Dependencies loaded...")


class CommandParams:
    """Parse incoming command and parameters"""

    MAX_NUMBER_OF_PARAMS = 6
    MIN_NUMBER_OF_PARAMS = 5

    def __init__(self, command, work_dir):

        self._parsing_error_message = None

        parts = command.split(maxsplit=CommandParams.MAX_NUMBER_OF_PARAMS)
        if (
            len(parts) < CommandParams.MIN_NUMBER_OF_PARAMS
            or len(parts) > CommandParams.MAX_NUMBER_OF_PARAMS
        ):
            self._parsing_error_message = f"ERROR: Invalid command format. Expected: <command> <task_id> <user_jwt> <lexis_project> <lexis_project_resource_id> or <command> <task_id> <user_jwt> <lexis_project> <lexis_project_resource_id> <optional_args>\nGot {parts}".encode(
                "utf-8"
            )

        self._optional_args = None
        if len(parts) == CommandParams.MAX_NUMBER_OF_PARAMS:
            (
                self._command,
                self._full_id,
                self._user_jwt,
                self._lexis_project,
                self._lexis_resource_id,
                self._optional_args,
            ) = parts
        else:
            (
                self._command,
                self._full_id,
                self._user_jwt,
                self._lexis_project,
                self._lexis_project_resource_id,
            ) = parts
        self._task_dir = work_dir / self._full_id

    @property
    def parsing_error_message(self):
        """When parsing error occurs, thense message is set and should be returned to client, otherwise None is returned

        :return: Parsing error message if parsing error occurs, otherwise None
        """
        return self._parsing_error_message

    @property
    def optional_args(self):
        """If number of parameters are larger then MIN_NUMBER_OF_PARAMS, then optional_args are set

        :return: Optional args if provided, otherwise None
        """
        return self._optional_args

    @property
    def command(self):
        """Command string

        :return: Command string
        """
        return self._command

    @property
    def full_id(self):
        """HEAppE Job Full Id

        :return: HEAppE Job Full Id
        """
        return self._full_id

    @property
    def lexis_project(self):
        """LEXIS Project short name

        :return: LEXIS Project short name
        """
        return self._lexis_project

    @property
    def lexis_project_resource_id(self):
        """LEXIS Resource ID, currently expected to be UUID, but kept generic in case of future changes
        :return: LEXIS Resource ID
        """
        return self._lexis_project_resource_id

    @property
    def task_dir(self):
        """Path to directory where all files related to the job execution are stored, e.g. backend.pkl, run_kwargs.pkl, circuit_1.qasm, etc.
        :return: Task directory path
        """
        return self._task_dir

    @property
    def user_jwt(self):
        """JWT token of the job submitter, expected to be HEAppE user JWT, but kept generic in case of future changes

        :return: User JWT token
        """
        return self._user_jwt

    def parsing_error(self):
        """Check if parsing error occurs during initialization of CommandParams object
        :return: Is parsing error
        """
        return bool(self._parsing_error_message)

    def verify_user_jwt(self) -> bool:
        """Verify whether given jwt is valid

        :return: Is Valid (not expired and correctly decodable)
        """
        try:
            decoded = jwt.decode(self._user_jwt, options={"verify_signature": False})
            exp_timestamp = decoded.get("exp")
            if exp_timestamp and datetime.fromtimestamp(
                exp_timestamp, tz=timezone.utc
            ) < datetime.now(timezone.utc):
                return False
            return True
        except Exception as e:
            print(f"Error decoding JWT: {e}", file=sys.stderr)
            return False


class IQMBackendService:
    def __init__(self, socket_path: str, work_dir: str):
        self.socket_path = socket_path
        self.work_dir = Path(work_dir)

        self._backend_cache = TTLCache(maxsize=1024, ttl=3600)
        self._consumption_cache = TTLCache(
            maxsize=8000, ttl=24 * 3600
        )  # keeping consumption info for 24h, as it is needed for checking consumption of past jobs when new job is submitted
        self._pulla_cache = TTLCache(maxsize=1024, ttl=3600)
        self._calibration_set_cache = TTLCache(maxsize=1024, ttl=3600)
        self._dynamic_quantum_architecture_cache = TTLCache(maxsize=1024, ttl=3600)

        self._kafka_producer = initializeKafkaProducer()

    @staticmethod
    def save_python_obj(path, obj, use_dill=False):
        with open(path, "wb") as f:
            if use_dill:
                dill.dump(obj, f, protocol=dill.HIGHEST_PROTOCOL)
            else:
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load_python_obj(path, use_dill=False):
        with open(path, "rb") as f:
            if use_dill:
                return dill.load(f, encoding="utf-8")
            return pickle.load(f, encoding="utf-8")

    @staticmethod
    def get_accounting_info(command_params: CommandParams) -> AccountingInfo | str:
        """Get all AccountingInfo for submitter

        :param command_params: _description_
        :return: Returns AccountingInfo object or error message string if submitter info cannot be obtained for any reason (invalid user JWT, failure to fetch info from HEAppE, etc.)
        """
        try:
            accounting_info = AccountingInfo(
                user_jwt=command_params.user_jwt,
                submitter_email=None,  # will be loaded by fetch_submitter_info_from_heappe method
                lexis_project=command_params.lexis_project,
                lexis_project_resource_id=command_params.lexis_project_resource_id,
            )

            email = accounting_info.decode_user_jwt_and_verify()
            if not email:
                print("User JWT is invalid or expired", file=sys.stderr)
                return None

            if not accounting_info.fetch_all_accounting_info(command_params.full_id):
                print(
                    "Failed to fetch all accounting info for submitter", file=sys.stderr
                )
                return None

            # FIXME: fix after HEAppE /heappe/JobReporting/JobsDetailedReport will be ready
            # if email != accounting_info.submitter_email:
            #     print(f"Email in JWT ({email}) does not match submitter email ({accounting_info.submitter_email})", file=sys.stderr)
            #     return False

            return accounting_info

        except Exception as e:
            import traceback

            traceback.print_exc(file=sys.stderr)
            print(f"Error getting accounting info: {e}", file=sys.stderr)
            return None

    def start(self):
        # Remove socket file if it exists
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        # Create Unix domain socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(QAAS_ALLOWED_CLIENT_COUNT)

        print(f"Service listening on {self.socket_path}")

        try:
            while True:
                conn, _ = server.accept()
                try:
                    self.handle_connection(conn)
                except Exception as e:
                    print(f"Error handling connection: {e}", file=sys.stderr)
                    try:
                        conn.sendall(f"ERROR: {str(e)}\n".encode())
                    except Exception:
                        pass
                finally:
                    conn.close()
        finally:
            server.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

    def handle_connection(self, conn):
        try:
            data = conn.recv(5 * 4096).decode().strip()
            if not data:
                return

            command_params = CommandParams(data, self.work_dir)
            if command_params.parsing_error():
                conn.sendall(command_params.parsing_error_message)
                return

            ACCOUNTED_COMMANDS = ["backend_run", "pulla_submit_playlist"]

            # Disabled commands
            if command_params.command in ["pulla_submit_playlist", "pulla_init"]:
                conn.sendall(b"COMMAND DISABLED")
                return

            # Get submitter, otherwise stop and fail
            if command_params.command in ACCOUNTED_COMMANDS:
                # Try to get submitter info from cache, otherwise fetch from HEAppE and cache it. If not found, return error
                accounting_info = IQMBackendService.get_accounting_info(command_params)

                if isinstance(accounting_info, str) or accounting_info is None:
                    conn.sendall(
                        f"ERROR: Unable to get job submitter info -- {accounting_info if isinstance(accounting_info, str) else 'Unknown error'}\n".encode()
                    )
                    return
                 
                print(
                    f"accountinfo: {accounting_info.aggregation_name}, {accounting_info.resource_name}, {accounting_info.lexis_project}, {accounting_info.decode_user_jwt_email()}",
                    file=sys.stderr,
                )
                  
                self._consumption_cache[command_params.full_id] = (
                    accounting_info  # submitter and accounting_string
                )

                try:
                    consumption = fetch_current_resource_consumption(accounting_info)
                except RuntimeError as e:
                    import traceback

                    traceback.print_exc(file=sys.stderr)
                    print(f"Error checking resource consumption: {e}", file=sys.stderr)
                    conn.sendall(
                        "ERROR: Error while fetching consumption of selected resouurce!\n".encode()
                    )
                    return

                if consumption > accounting_info.allocation_amount:
                    # Consumption exceeded limits, allow job
                    conn.sendall(
                        f"ERROR: Current resource consumption {consumption:.2f} exceeds allocation {accounting_info.allocation_amount:.2f}\n".encode()
                    )
                    return

                # Consumption is within limits, allow job

            # Handle command
            if command_params.command == "backend_init":
                # Not accounted
                if command_params.optional_args is None:
                    conn.sendall(b"ERROR: Missing backend name.")
                else:
                    opt_args_split = command_params.optional_args.split(",")
                    self.backend_init(
                        command_params.task_dir, command_params.full_id, *opt_args_split
                    )
                    conn.sendall(b"DONE\n")
            elif command_params.command == "backend_run":
                # Accounted
                self.backend_run(command_params.task_dir, command_params.full_id)
                conn.sendall(b"DONE\n")
            elif command_params.command == "pulla_init":
                # Not accounted
                self.pulla_init(command_params.task_dir, command_params.full_id)
                conn.sendall(b"DONE\n")
            elif command_params.command == "pulla_submit_playlist":
                # Accounted
                self.pulla_submit_playlist(
                    command_params.task_dir, command_params.full_id
                )
                conn.sendall(b"DONE\n")
            elif command_params.command == "get_calibration_set":
                # Not accounted
                self.get_calibration_set(
                    command_params.task_dir,
                    command_params.full_id,
                    None
                    if not command_params.optional_args
                    else (
                        UUID(command_params.optional_args)
                        if command_params.optional_args
                        and command_params.optional_args != "None"
                        and command_params.optional_args != "default"
                        else None
                    ),
                )
                conn.sendall(b"DONE\n")
            elif command_params.command == "get_dynamic_quantum_architecture":
                # Not accounted
                self.get_dynamic_quantum_architecture(
                    command_params.task_dir,
                    command_params.full_id,
                    None
                    if not command_params.optional_args
                    else (
                        UUID(command_params.optional_args)
                        if command_params.optional_args
                        and command_params.optional_args != "None"
                        and command_params.optional_args != "default"
                        else None
                    ),
                )
                conn.sendall(b"DONE\n")
            else:
                # Unknown command
                conn.sendall(
                    f"ERROR: Unknown command '{command_params.command}'. Valid commands: backend_init, backend_run, pulla_init, pulla_submit_playlist, get_calibration_set, get_dynamic_quantum_architecture\n".encode()
                )

            if command_params.command in ACCOUNTED_COMMANDS:
                record_consumption_usage(
                    self._kafka_producer,
                    accounting_info,
                    self._consumption_cache.get(command_params.full_id, 0.0),
                )

        except UnicodeDecodeError as e:
            error_msg = f"ERROR: Failed to decode message: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error UnicodeDecodeError")

        except FileNotFoundError as e:
            error_msg = f"ERROR: File not found: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error FileNotFoundError")

        except ValueError as e:
            error_msg = f"ERROR: Invalid value: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error ValueError")

        except Exception as e:
            error_msg = f"ERROR: {type(e).__name__}: {str(e)}\n"
            print(
                f"Unhandled exception in handle_connection: {error_msg}",
                file=sys.stderr,
            )
            import traceback

            traceback.print_exc(file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error!!!")

    def get_calibration_set(
        self, task_dir: Path, task_id: str, calibration_set_id: UUID | None
    ):
        _calibration_set_id = (
            "default" if calibration_set_id is None else calibration_set_id
        )
        calibration_set = self._calibration_set_cache.get(calibration_set_id, None)

        if not calibration_set:
            server_url = os.getenv("IQM_SERVER_URL")
            if not server_url:
                raise ValueError("IQM_SERVER_URL environment variable is required")

            c = IQMClient(server_url)
            self._calibration_set_cache[_calibration_set_id] = c.get_calibration_set(
                _calibration_set_id
            )

        IQMBackendService.save_python_obj(
            task_dir / "calibration_set.pkl",
            self._calibration_set_cache[_calibration_set_id],
            use_dill=True,
        )

        print(f"Calibration set get and saved for task {task_id}")

    def get_dynamic_quantum_architecture(
        self, task_dir: Path, task_id: str, calibration_set_id: UUID | None
    ):
        _calibration_set_id = (
            "default" if calibration_set_id is None else calibration_set_id
        )
        calibration_set = self._dynamic_quantum_architecture_cache.get(
            _calibration_set_id, None
        )

        if not calibration_set:
            server_url = os.getenv("IQM_SERVER_URL")
            if not server_url:
                raise ValueError("IQM_SERVER_URL environment variable is required")

            c = IQMClient(server_url)
            self._dynamic_quantum_architecture_cache[_calibration_set_id] = (
                c.get_dynamic_quantum_architecture(_calibration_set_id)
            )

        IQMBackendService.save_python_obj(
            task_dir / "dynamic_quantum_architecture.pkl",
            self._dynamic_quantum_architecture_cache[_calibration_set_id],
            use_dill=True,
        )

        print(f"Dynamic architecture retrieved and saved for task {task_id}")

    def backend_init(
        self,
        task_dir: Path,
        task_id: str,
        backend_name: str,
        calibration_set_id: UUID | None = None,
    ):
        print(f"Initializing backend for task {task_id}")

        server_url = os.getenv("IQM_SERVER_URL")
        if not server_url:
            raise ValueError("IQM_SERVER_URL environment variable is required")

        quantum_computer = os.getenv("IQM_QUANTUM_COMPUTER")  # Make more generic

        provider = IQMProvider(url=server_url, quantum_computer=quantum_computer)
        backend = provider.get_backend(
            name=backend_name,
            calibration_set_id=UUID(calibration_set_id) if calibration_set_id else None,
        )

        # Cache backend
        self._backend_cache[task_id] = backend

        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        iqm_attrs = {
            attr: getattr(backend.target, attr)
            for attr in dir(backend.target)
            if attr.startswith("iqm_") and not attr.startswith("__")
        }

        # Save to task directory
        IQMBackendService.save_python_obj(
            task_dir / "backend.pkl", backend, use_dill=False
        )
        IQMBackendService.save_python_obj(
            task_dir / "iqm_target_attrs.pkl", iqm_attrs, use_dill=False
        )

        print(f"Backend initialized and saved for task {task_id}")

    def backend_run(self, task_dir: Path, task_id: str):
        print(f"Running job for task {task_id} - backend_run")

        backend_run_initialization_started = time.time()

        # Load run_kwargs
        run_kwargs_path = task_dir / "run_kwargs.pkl"
        if not run_kwargs_path.exists():
            raise FileNotFoundError(f"run_kwargs.pkl not found in {task_dir}")

        run_kwargs = IQMBackendService.load_python_obj(run_kwargs_path, use_dill=True)

        # Load or get cached backend
        backend = self._backend_cache.get(task_id)
        if backend is None:
            backend_path = task_dir / "backend.pkl"
            if not backend_path.exists():
                raise FileNotFoundError(f"backend.pkl not found in {task_dir}")

            backend: IQMBackend = IQMBackendService.load_python_obj(
                backend_path, use_dill=False
            )
            # Fixes iqm_ attrs of IQMTarget
            iqm_target_attrs = IQMBackendService.load_python_obj(
                task_dir / "iqm_target_attrs.pkl", use_dill=False
            )
            for attr, value in iqm_target_attrs.items():
                setattr(backend.target, attr, value)

            self._backend_cache[task_id] = backend

        # Load circuits
        circuits_qasm: list[QuantumCircuit] = []

        for file_path in sorted(task_dir.glob("circuit_*.qasm")):
            circuits_qasm.append(qasm3load(file_path))

        # for file_path in sorted(task_dir.glob('circuit_*.pkl')):
        #     circuits_qiskit.append(IQMBackendService.load_python_obj(file_path, use_dill=True))

        # circuits = [*circuits_qiskit, *circuits_qasm]
        circuits = circuits_qasm

        backend_run_initialization_ended = time.time()
        backend_run_initialization_runtime = (
            backend_run_initialization_ended - backend_run_initialization_started
        )

        # Transpilation - DISABLED FEATURE
        backend_run_transpilation_started = backend_run_initialization_ended

        # Run job
        run_input = circuits[0] if len(circuits) == 1 else circuits

        backend_run_transpilation_ended = time.time()
        backend_run_transpilation_runtime = (
            backend_run_transpilation_ended - backend_run_transpilation_started
        )

        iqm_client_run_started = backend_run_transpilation_ended
        job: IQMJob = backend.run(run_input, **run_kwargs)
        iqm_client_run_ended = time.time()

        print(f"Job submitted: {job.job_id()}")
        iqm_client_results_fetching_started = time.time()
        result = job.result(timeout=1740)  # 29 minutes to add safe margin
        iqm_client_run_results_fetching_ended = time.time()

        iqm_client_results_fetching_runtime = (
            iqm_client_run_results_fetching_ended - iqm_client_results_fetching_started
        )
        iqm_client_job_runtime = (
            iqm_client_run_results_fetching_ended - iqm_client_run_started
        )

        # Add timing information
        job.remote_initialization_runtime = backend_run_initialization_started
        job.remote_backend_run_transpilation_runtime = backend_run_transpilation_runtime
        job.remote_iqm_client_job_runtime = iqm_client_job_runtime
        job.remote_iqm_client_results_fetching_runtime = (
            iqm_client_results_fetching_runtime
        )

        # Add timestamps of events
        job.events = {
            "backend_run_initialization_started": backend_run_initialization_started,
            "backend_run_initialization_ended": backend_run_initialization_ended,
            "backend_run_transpilation_started": backend_run_transpilation_started,
            "backend_run_transpilation_ended": backend_run_transpilation_started,
            "iqm_client_run_started": iqm_client_run_started,
            "iqm_client_run_ended": iqm_client_run_ended,
            "iqm_client_results_fetching_started": iqm_client_results_fetching_started,
            "iqm_client_run_results_fetching_ended": iqm_client_run_results_fetching_ended,
            "backend_run_postprocessing_started": None,
            "backend_run_postprocessing_ended": None,
        }

        # Extract hardware runtime from timeline
        exec_started_timeline = None
        exec_ended_timeline = None
        for entry in result.timeline:  # NOTE: currently supported only single submit
            if entry.status == "execution_started":
                exec_started_timeline = entry
            if entry.status == "execution_ended":
                exec_ended_timeline = entry
            if exec_started_timeline and exec_ended_timeline:
                break

        if exec_started_timeline and exec_ended_timeline:
            job.remote_hw_runtime = (
                exec_ended_timeline.timestamp - exec_started_timeline.timestamp
            ).total_seconds()
            #########################################
            # RECORD USAGE INFO IN ACCOUNTING CACHE #
            #########################################
            self._consumption_cache[task_id] = job.remote_hw_runtime
        # Check status
        if job.status() == IQMJobStatus.FAILED or not result.success:
            raise Exception(f"Job failed: {job.error_message() or 'None'}")

        # Save results
        backend_run_postprocessing_started = time.time()

        IQMBackendService.save_python_obj(
            task_dir / "results.pkl", result, use_dill=True
        )
        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        iqm_attrs = {
            attr: getattr(backend.target, attr)
            for attr in dir(backend.target)
            if attr.startswith("iqm_") and not attr.startswith("__")
        }
        IQMBackendService.save_python_obj(
            task_dir / "backend.pkl", backend, use_dill=False
        )
        IQMBackendService.save_python_obj(
            task_dir / "iqm_target_attrs.pkl", iqm_attrs, use_dill=False
        )
        IQMBackendService.save_python_obj(
            task_dir / "transpiled_circuits.pkl", circuits, use_dill=True
        )

        backend_run_postprocessing_ended = time.time()
        backend_run_postprocessing_runtime = (
            backend_run_postprocessing_ended - backend_run_postprocessing_started
        )
        job.remote_backend_run_postprocessing_runtime = (
            backend_run_postprocessing_runtime
        )
        job.remote_backend_runtime = (
            backend_run_initialization_runtime
            + backend_run_transpilation_runtime
            + iqm_client_job_runtime
            + backend_run_postprocessing_runtime
        )
        job.events["backend_run_postprocessing_started"] = (
            backend_run_postprocessing_started
        )
        job.events["backend_run_postprocessing_ended"] = (
            backend_run_postprocessing_ended
        )

        IQMBackendService.save_python_obj(task_dir / "job.pkl", job, use_dill=True)

        # record postprocessing time as last one

        print(f"Task {task_id} completed in {job.remote_backend_runtime:.2f}s ")

    def pulla_init(self, task_dir: Path, task_id: str):
        print(f"Initializing Pulla instance for task {task_id}")

        server_url = os.getenv("IQM_SERVER_URL")
        if not server_url:
            raise ValueError("IQM_SERVER_URL environment variable is required")

        quantum_computer = os.getenv("IQM_QUANTUM_COMPUTER")

        p = Pulla(server_url, quantum_computer=quantum_computer)

        channel_prop, component_channels = p.get_channel_properties()

        pulla_data = {
            "calibration_sets": p._calibration_data_provider._calibration_sets,
            "station_control_settings": p._get_station_control_settings(),
            "chip_label": p.get_chip_label(),
            "channel_properties": channel_prop,
            "component_channels": component_channels,
            "chip_design_record": p._iqm_server_client.get_chip_design_records()[0],
            "duts": p._iqm_server_client.get_duts(),
        }
        # Cache backend
        self._pulla_cache[task_id] = p

        # Save to task directory
        IQMBackendService.save_python_obj(
            task_dir / "pulla_data.pkl", pulla_data, use_dill=True
        )
        IQMBackendService.save_python_obj(task_dir / "pulla.pkl", p, use_dill=True)

        print(f"Pulla initialized and saved for task {task_id}")

    def pulla_submit_playlist(self, task_dir: Path, task_id: str):
        print(f"Running job for task {task_id} - pulla_submit_playlist")

        pulla_submit_pl_initialization_started = time.time()

        # Load sweep
        sweep_path = task_dir / "sweep.pkl"
        if not sweep_path.exists():
            raise FileNotFoundError(f"circuits.pkl not found in {task_dir}")

        sweep = IQMBackendService.load_python_obj(sweep_path, use_dill=True)

        # Context
        context_path = task_dir / "run_kwargs.pkl"
        if not context_path.exists():
            raise FileNotFoundError(
                f"run_kwargs.pkl not found in {task_dir}. Should contain context of Pulla compiler required for submission"
            )

        context = IQMBackendService.load_python_obj(context_path, use_dill=True)

        # Load or get cached pulla
        pulla: Pulla = self._pulla_cache.get(task_id)
        if pulla is None:
            pulla_path = task_dir / "pulla.pkl"
            if not pulla_path.exists():
                raise FileNotFoundError(f"pulla.pkl not found in {task_dir}")

            pulla = IQMBackendService.load_python_obj(pulla_path, use_dill=True)
            self._pulla_cache[task_id] = pulla

        pulla_submit_pl_initialization_ended = time.time()
        pulla_submit_pl_initialization_runtime = (
            pulla_submit_pl_initialization_ended
            - pulla_submit_pl_initialization_started
        )

        iqm_client_run_started = pulla_submit_pl_initialization_ended
        job_data = pulla._iqm_server_client.submit_sweep(sweep)
        iqm_client_run_ended = time.time()
        iqm_client_job_runtime = iqm_client_run_ended - iqm_client_run_started

        iqm_client_results_fetching_started = time.time()
        sw_job = SweepJob(
            data=job_data,
            _pulla=pulla,
            _context=deepcopy(context),
        )
        print(f"Job submitted: {sw_job.job_id}")
        sw_job.wait_for_completion(timeout_secs=0.0)
        result = sw_job.result()
        iqm_client_run_results_fetching_ended = time.time()
        iqm_client_results_fetching_runtime = (
            iqm_client_run_results_fetching_ended - iqm_client_results_fetching_started
        )

        # Add timing information
        sw_job.remote_initialization_runtime = pulla_submit_pl_initialization_started
        sw_job.remote_backend_run_transpilation_runtime = None
        sw_job.remote_iqm_client_job_runtime = iqm_client_job_runtime
        sw_job.remote_iqm_client_results_fetching_runtime = (
            iqm_client_results_fetching_runtime
        )

        # Add timestamps of events
        sw_job.events = {
            "backend_run_initialization_started": pulla_submit_pl_initialization_started,
            "backend_run_initialization_ended": pulla_submit_pl_initialization_ended,
            "iqm_client_run_started": iqm_client_run_started,
            "iqm_client_run_ended": iqm_client_run_ended,
            "iqm_client_results_fetching_started": iqm_client_results_fetching_started,
            "iqm_client_run_results_fetching_ended": iqm_client_run_results_fetching_ended,
            "backend_run_postprocessing_started": None,
            "backend_run_postprocessing_ended": None,
        }

        # Extract hardware runtime from timeline
        exec_started_timeline = None
        exec_ended_timeline = None
        for entry in sw_job.data.timeline:
            if entry.status == "execution_started":
                exec_started_timeline = entry
            if entry.status == "execution_ended":
                exec_ended_timeline = entry
            if exec_started_timeline and exec_ended_timeline:
                break

        if exec_started_timeline and exec_ended_timeline:
            sw_job.remote_hw_runtime = (
                exec_ended_timeline.timestamp - exec_started_timeline.timestamp
            ).total_seconds()
            #########################################
            # RECORD USAGE INFO IN ACCOUNTING CACHE #
            #########################################
            self._consumption_cache[task_id] = sw_job.remote_hw_runtime

        # Check status
        if sw_job.status == IQMJobStatus.FAILED or not result.success:
            raise Exception(
                f"Job failed: {sw_job._errors[0] if sw_job._errors else 'Unknown sweep job error'}"
            )

        # Save results
        pulla_submit_pl_postprocessing_started = time.time()

        IQMBackendService.save_python_obj(
            task_dir / "results.pkl", result, use_dill=True
        )

        pulla_submit_pl_postprocessing_ended = time.time()
        backend_run_postprocessing_runtime = (
            pulla_submit_pl_postprocessing_ended
            - pulla_submit_pl_postprocessing_started
        )
        sw_job.remote_backend_run_postprocessing_runtime = (
            backend_run_postprocessing_runtime
        )
        sw_job.remote_backend_runtime = (
            pulla_submit_pl_initialization_runtime
            + iqm_client_job_runtime
            + backend_run_postprocessing_runtime
        )
        sw_job.events["backend_run_postprocessing_started"] = (
            pulla_submit_pl_postprocessing_started
        )
        sw_job.events["backend_run_postprocessing_ended"] = (
            pulla_submit_pl_postprocessing_ended
        )
        IQMBackendService.save_python_obj(task_dir / "job.pkl", sw_job, use_dill=True)
