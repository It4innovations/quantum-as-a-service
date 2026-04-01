#!/usr/bin/env python3

import os
import sys
import pickle
import dill
import time
import socket
import json
from copy import deepcopy
from pathlib import Path
from typing import Optional
from uuid import UUID
from cachetools import TTLCache
from qiskit import QuantumCircuit
from iqm.qiskit_iqm import transpile_to_IQM, IQMBackend, IQMProvider
from iqm.qiskit_iqm.iqm_job import IQMJob
from iqm.iqm_client import JobStatus as IQMJobStatus
from iqm.iqm_server_client.models import TimelineEntry
from iqm.pulla.pulla import SweepJob, Pulla, CalibrationDataProvider
from iqm.iqm_client import IQMClient

print("Dependencies loaded...")
QAAS_ALLOWED_CLIENT_COUNT = os.getenv('QAAS_ALLOWED_CLIENT_COUNT',100)
print(f"Will accept {QAAS_ALLOWED_CLIENT_COUNT} client at max")

class IQMBackendService:
    def __init__(self, socket_path: str, work_dir: str):
        self.socket_path = socket_path
        self.work_dir = Path(work_dir)

        self.backend_cache = TTLCache(maxsize=1024, ttl=3600)
        self.pulla_cache = TTLCache(maxsize=1024, ttl=3600)
        self.calibration_set_cache = TTLCache(maxsize=1024, ttl=3600)
        self.dynamic_quantum_architecture_cache = TTLCache(maxsize=1024, ttl=3600)
    
    @staticmethod
    def save_python_obj(path, obj, use_dill=False):
        with open(path, 'wb') as f:
            if use_dill:
                dill.dump(obj, f, protocol=dill.HIGHEST_PROTOCOL)
            else:
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    
    @staticmethod
    def load_python_obj(path, use_dill=False):
        with open(path, 'rb') as f:
            if use_dill:
                return dill.load(f, encoding='utf-8')
            return pickle.load(f, encoding='utf-8')
    
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
                    except:
                        pass
                finally:
                    conn.close()
        finally:
            server.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
    
    def handle_connection(self, conn):
        try:
            data = conn.recv(4096).decode().strip()
            if not data:
                return
            
            parts = data.split(maxsplit=3)
            if len(parts) < 2 or len(parts) > 3:
                conn.sendall(f"ERROR: Invalid command format. Expected: <command> <task_id> or <command> <task_id> <optional_args>\nGot {parts}".encode('utf-8'))
                return
            
            optional_args = None
            if len(parts) == 3:
                command, full_id, optional_args = parts
            else:
                command, full_id = parts
            task_dir = self.work_dir / full_id
            
            if command == "backend_init":
                if len(parts) < 3:
                    conn.sendall("ERROR: Missing backend name.")
                else:
                    opt_args_splitted = optional_args.split(",")
                    self.backend_init(task_dir, full_id,  *opt_args_splitted)
                    conn.sendall(b"DONE\n")
            elif command == "backend_run":
                self.backend_run(task_dir, full_id)
                conn.sendall(b"DONE\n")
            elif command == "pulla_init":
                self.pulla_init(task_dir, full_id)
                conn.sendall(b"DONE\n")
            elif command == "pulla_submit_playlist":
                self.pulla_submit_playlist(task_dir, full_id)
                conn.sendall(b"DONE\n")
            elif command == "get_calibration_set":
                self.get_calibration_set(task_dir, full_id, None if len(parts) == 2 else (UUID(optional_args) if optional_args and optional_args != "None" and optional_args != "default" else None))
                conn.sendall(b"DONE\n")
            elif command == "get_dynamic_quantum_architecture":
                self.get_dynamic_quantum_architecture(task_dir, full_id, None if len(parts) == 2 else (UUID(optional_args) if optional_args and optional_args != "None" and optional_args != "default" else None))
                conn.sendall(b"DONE\n")
            else:
                conn.sendall(f"ERROR: Unknown command '{command}'. Valid commands: backend_init, backend_run, pulla_init, pulla_submit_playlist, get_calibration_set, get_dynamic_quantum_architecture\n".encode())
        
        except UnicodeDecodeError as e:
            error_msg = f"ERROR: Failed to decode message: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error UnicodeDecodeError")
        
        except FileNotFoundError as e:
            error_msg = f"ERROR: File not found: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error FileNotFoundError")
        
        except ValueError as e:
            error_msg = f"ERROR: Invalid value: {str(e)}\n"
            print(error_msg, file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error ValueError")
        
        except Exception as e:
            error_msg = f"ERROR: {type(e).__name__}: {str(e)}\n"
            print(f"Unhandled exception in handle_connection: {error_msg}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            try:
                conn.sendall(error_msg.encode())
            except Exception:
                conn.sendall("Unexpected error!!!")
    
    def get_calibration_set(self, task_dir: Path, task_id: str, calibration_set_id: UUID|None):
        _calibration_set_id="default" if calibration_set_id is None else calibration_set_id
        calibration_set = self.calibration_set_cache.get(calibration_set_id, None)
        
        if not calibration_set:
            server_url = os.getenv('IQM_SERVER_URL')
            if not server_url:
                raise ValueError("IQM_SERVER_URL environment variable is required")
            
            c = IQMClient(server_url)
            self.calibration_set_cache[_calibration_set_id] = c.get_calibration_set(_calibration_set_id)
        
        IQMBackendService.save_python_obj(task_dir / "calibration_set.pkl", self.calibration_set_cache[_calibration_set_id], use_dill=True)
            
        print(f"Calibration set get and saved for task {task_id}")
    
    def get_dynamic_quantum_architecture(self, task_dir: Path, task_id: str, calibration_set_id: UUID|None):
        _calibration_set_id="default" if calibration_set_id is None else calibration_set_id
        calibration_set = self.dynamic_quantum_architecture_cache.get(_calibration_set_id, None)
        
        if not calibration_set:
            server_url = os.getenv('IQM_SERVER_URL')
            if not server_url:
                raise ValueError("IQM_SERVER_URL environment variable is required")
            
            c = IQMClient(server_url)
            self.dynamic_quantum_architecture_cache[_calibration_set_id] = c.get_dynamic_quantum_architecture(_calibration_set_id)
        
        IQMBackendService.save_python_obj(task_dir / "dynamic_quantum_architecture.pkl", self.dynamic_quantum_architecture_cache[_calibration_set_id], use_dill=True)
            
        print(f"Dynamic architecture retrieved and saved for task {task_id}")
        
    
    def backend_init(self, task_dir: Path, task_id: str, backend_name:str, calibration_set_id: UUID|None=None):
        print(f"Initializing backend for task {task_id}")
        
        server_url = os.getenv('IQM_SERVER_URL')
        if not server_url:
            raise ValueError("IQM_SERVER_URL environment variable is required")
        
        quantum_computer = os.getenv('IQM_QUANTUM_COMPUTER') # Make more generic
        
        provider = IQMProvider(url=server_url, quantum_computer=quantum_computer)
        backend = provider.get_backend(
            name=backend_name,
            calibration_set_id=UUID(calibration_set_id) if calibration_set_id else None
        )
        
        # Cache backend
        self.backend_cache[task_id] = backend
        
        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        iqm_attrs = {
            attr: getattr(backend.target, attr)
            for attr in dir(backend.target)
            if attr.startswith('iqm_') and not attr.startswith('__')
        }
        
        # Save to task directory
        IQMBackendService.save_python_obj(task_dir / "backend.pkl", backend, use_dill=False)
        IQMBackendService.save_python_obj(task_dir / "iqm_target_attrs.pkl", iqm_attrs, use_dill=False)
        
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
        backend = self.backend_cache.get(task_id)
        if backend is None:
            backend_path = task_dir / "backend.pkl"
            if not backend_path.exists():
                raise FileNotFoundError(f"backend.pkl not found in {task_dir}")
            
            backend:IQMBackend = IQMBackendService.load_python_obj(backend_path, use_dill=False)
            # Fixes iqm_ attrs of IQMTarget
            iqm_target_attrs = IQMBackendService.load_python_obj(task_dir / "iqm_target_attrs.pkl", use_dill=False)
            for attr, value in iqm_target_attrs.items():
                setattr(backend.target, attr, value)
            
            self.backend_cache[task_id] = backend
        
        # Load circuits
        circuits_qasm = []
        circuits_qiskit = []
        
        for file_path in sorted(task_dir.glob('circuit_*.qasm')):
            with open(file_path, 'r', encoding='utf-8') as f:
                circuits_qasm.append(QuantumCircuit.from_qasm_str(f.read()))
        
        for file_path in sorted(task_dir.glob('circuit_*.pkl')):
            circuits_qiskit.append(IQMBackendService.load_python_obj(file_path, use_dill=True))
        
        circuits = [*circuits_qiskit, *circuits_qasm]
        
        backend_run_initialization_ended = time.time()
        backend_run_initialization_runtime = backend_run_initialization_ended - backend_run_initialization_started
        
        # Transpilation
        backend_run_transpilation_started = backend_run_initialization_ended
        do_transpile = run_kwargs.pop('do_transpile', False)
        
        if do_transpile:
            transpile_args = {
                'target': run_kwargs.pop('target', None),
                'perform_move_routing': run_kwargs.pop('perform_move_routing', True),
                'optimize_single_qubits': run_kwargs.pop('optimize_single_qubits', True),
                'ignore_barriers': run_kwargs.pop('ignore_barriers', False),
                'remove_final_rzs': run_kwargs.pop('remove_final_rzs', True),
                'existing_moves_handling': run_kwargs.pop('existing_moves_handling', None),
                'restrict_to_qubits': run_kwargs.pop('restrict_to_qubits', None),
                'initial_layout': run_kwargs.pop('initial_layout', None),
                'basis_gates': run_kwargs.pop('basis_gates', None),
                'coupling_map': run_kwargs.pop('coupling_map', True),
                'instruction_durations': run_kwargs.pop('instruction_durations', True),
                'inst_map': run_kwargs.pop('inst_map', False),
                'dt': run_kwargs.pop('dt', True),
                'timing_constraints': run_kwargs.pop('timing_constraints', None),
                'optimization_level': run_kwargs.pop('optimization_level', None),
                'optimization_method': run_kwargs.pop('optimization_method', None),
            }
            
            circuits = [transpile_to_IQM(circuit, backend, **transpile_args) 
                       for circuit in circuits]
        
        
        # Run job
        run_input = circuits[0] if len(circuits) == 1 else circuits
        run_params = {
            'shots': run_kwargs.pop('shots', 1024),
            **run_kwargs.get('run_options', {})
        }
        
        backend_run_transpilation_ended = time.time()
        backend_run_transpilation_runtime = backend_run_transpilation_ended - backend_run_transpilation_started
        
        iqm_client_run_started = backend_run_transpilation_ended
        job:IQMJob = backend.run(run_input, **run_params)
        iqm_client_run_ended = time.time()
        
        print(f"Job submitted: {job.job_id}")
        iqm_client_results_fetching_started = time.time()
        result = job.result(timeout=1740) # 29 minutes to add safe margin
        iqm_client_run_results_fetching_ended = time.time()
        
        iqm_client_results_fetching_runtime = iqm_client_run_results_fetching_ended - iqm_client_results_fetching_started
        iqm_client_job_runtime = iqm_client_run_results_fetching_ended - iqm_client_run_started
        
        # Add timing information
        job.remote_initialization_runtime = backend_run_initialization_started
        job.remote_backend_run_transpilation_runtime = backend_run_transpilation_runtime
        job.remote_iqm_client_job_runtime = iqm_client_job_runtime
        job.remote_iqm_client_results_fetching_runtime = iqm_client_results_fetching_runtime
        
        # Add timestamps of events
        job.events = {
            'backend_run_initialization_started': backend_run_initialization_started,
            'backend_run_initialization_ended': backend_run_initialization_ended,
            'backend_run_transpilation_started': backend_run_transpilation_started,
            'backend_run_transpilation_ended': backend_run_transpilation_started,
            'iqm_client_run_started': iqm_client_run_started,
            'iqm_client_run_ended': iqm_client_run_ended,
            'iqm_client_results_fetching_started': iqm_client_results_fetching_started,
            'iqm_client_run_results_fetching_ended': iqm_client_run_results_fetching_ended,
            'backend_run_postprocessing_started': None,
            'backend_run_postprocessing_ended': None
        }
        
        # Extract hardware runtime from timeline
        exec_started_timeline = None
        exec_ended_timeline = None
        for entry in result.timeline: #NOTE: currently supported only single submit
            if entry.status == "execution_started":
                exec_started_timeline = entry
            if entry.status == "execution_ended":
                exec_ended_timeline = entry
            if exec_started_timeline and exec_ended_timeline:
                break
        
        if exec_started_timeline and exec_ended_timeline:
            job.remote_hw_runtime = (exec_ended_timeline.timestamp - 
                                    exec_started_timeline.timestamp).total_seconds()
        
        # Check status
        if job.status() == IQMJobStatus.FAILED or not result.success:
            raise Exception(f"Job failed: {job.error_message() or 'None'}")
        
        # Save results
        backend_run_postprocessing_started = time.time()
        
        
        IQMBackendService.save_python_obj(task_dir / 'results.pkl', result, use_dill=True)
        # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
        iqm_attrs = {
            attr: getattr(backend.target, attr)
            for attr in dir(backend.target)
            if attr.startswith('iqm_') and not attr.startswith('__')
        }
        IQMBackendService.save_python_obj(task_dir / "backend.pkl", backend, use_dill=False)
        IQMBackendService.save_python_obj(task_dir / "iqm_target_attrs.pkl", iqm_attrs, use_dill=False)
        IQMBackendService.save_python_obj(task_dir / 'transpiled_circuits.pkl', circuits, use_dill=True)
        
        
        backend_run_postprocessing_ended = time.time()
        backend_run_postprocessing_runtime = backend_run_postprocessing_ended - backend_run_postprocessing_started
        job.remote_backend_run_postprocessing_runtime = backend_run_postprocessing_runtime
        job.remote_backend_runtime=backend_run_initialization_runtime + backend_run_transpilation_runtime + iqm_client_job_runtime + backend_run_postprocessing_runtime
        job.events['backend_run_postprocessing_started'] = backend_run_postprocessing_started
        job.events['backend_run_postprocessing_ended'] = backend_run_postprocessing_ended
        
        IQMBackendService.save_python_obj(task_dir / 'job.pkl', job, use_dill=True)
        
        # record postprocessing time as last one
        
        print(f"Task {task_id} completed in {job.remote_backend_runtime:.2f}s ")
    
    def pulla_init(self, task_dir: Path, task_id: str):
        print(f"Initializing Pulla instance for task {task_id}")
        
        server_url = os.getenv('IQM_SERVER_URL')
        if not server_url:
            raise ValueError("IQM_SERVER_URL environment variable is required")
        
        quantum_computer = os.getenv('IQM_QUANTUM_COMPUTER')
        
        p = Pulla(server_url, quantum_computer=quantum_computer)
        
        channel_prop, component_channels = p.get_channel_properties()
        
        pulla_data = {
            'calibration_sets':p._calibration_data_provider._calibration_sets,
            'station_control_settings':p._get_station_control_settings(),
            'chip_label':p.get_chip_label(),
            'channel_properties':channel_prop,
            'component_channels':component_channels,
            'chip_design_record':p._iqm_server_client.get_chip_design_records()[0],
            'duts':p._iqm_server_client.get_duts()
        }
        # Cache backend
        self.pulla_cache[task_id] = p
        
        # Save to task directory
        IQMBackendService.save_python_obj(task_dir / "pulla_data.pkl", pulla_data, use_dill=True)
        IQMBackendService.save_python_obj(task_dir / "pulla.pkl",p, use_dill=True)
        
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
            raise FileNotFoundError(f"run_kwargs.pkl not found in {task_dir}. Should contain context of Pulla compiler required for submission")
        
        context = IQMBackendService.load_python_obj(context_path, use_dill=True)
        
        # Load or get cached pulla
        pulla:Pulla = self.pulla_cache.get(task_id)
        if pulla is None:
            pulla_path = task_dir / "pulla.pkl"
            if not pulla_path.exists():
                raise FileNotFoundError(f"pulla.pkl not found in {task_dir}")
            
            pulla = IQMBackendService.load_python_obj(pulla_path, use_dill=True)
            self.pulla_cache[task_id] = pulla

        pulla_submit_pl_initialization_ended = time.time()
        pulla_submit_pl_initialization_runtime = pulla_submit_pl_initialization_ended - pulla_submit_pl_initialization_started
        
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
        iqm_client_results_fetching_runtime = iqm_client_run_results_fetching_ended - iqm_client_results_fetching_started
        
        # Add timing information
        sw_job.remote_initialization_runtime = pulla_submit_pl_initialization_started
        sw_job.remote_backend_run_transpilation_runtime = None
        sw_job.remote_iqm_client_job_runtime = iqm_client_job_runtime
        sw_job.remote_iqm_client_results_fetching_runtime = iqm_client_results_fetching_runtime
        
        # Add timestamps of events
        sw_job.events = {
            'backend_run_initialization_started': pulla_submit_pl_initialization_started,
            'backend_run_initialization_ended': pulla_submit_pl_initialization_ended,
            'iqm_client_run_started': iqm_client_run_started,
            'iqm_client_run_ended': iqm_client_run_ended,
            'iqm_client_results_fetching_started': iqm_client_results_fetching_started,
            'iqm_client_run_results_fetching_ended': iqm_client_run_results_fetching_ended,
            'backend_run_postprocessing_started': None,
            'backend_run_postprocessing_ended': None
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
            sw_job.remote_hw_runtime = (exec_ended_timeline.timestamp - 
                                    exec_started_timeline.timestamp).total_seconds()
        
        # Check status
        if sw_job.status == IQMJobStatus.FAILED or not result.success:
            raise Exception(f"Job failed: {sw_job.error_message() or 'None'}")
        
        # Save results
        pulla_submit_pl_postprocessing_started = time.time()
        
        IQMBackendService.save_python_obj(task_dir / 'results.pkl', result, use_dill=True)
        
        pulla_submit_pl_postprocessing_ended = time.time()
        backend_run_postprocessing_runtime = pulla_submit_pl_postprocessing_ended - pulla_submit_pl_postprocessing_started
        sw_job.remote_backend_run_postprocessing_runtime = backend_run_postprocessing_runtime
        sw_job.remote_backend_runtime=pulla_submit_pl_initialization_runtime + iqm_client_job_runtime + backend_run_postprocessing_runtime
        sw_job.events['backend_run_postprocessing_started'] = pulla_submit_pl_postprocessing_started
        sw_job.events['backend_run_postprocessing_ended'] = pulla_submit_pl_postprocessing_ended
        IQMBackendService.save_python_obj(task_dir / 'job.pkl', sw_job, use_dill=True)
        
        

def main():
    socket_path = os.getenv('IQM_SERVICE_SOCKET', '/tmp/iqm_backend.sock')
    work_dir = os.getenv('IQM_WORK_DIR', '/tmp/iqm_tasks')
    
    print("Starting server...")
    service = IQMBackendService(socket_path, work_dir)
    service.start()


if __name__ == "__main__":
    main()