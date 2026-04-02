QaaS - Quantum-as-a-Service for LEXIS Platform
============================================

Overview
--------

The ``qaas`` package provides access to quantum computing resources through the LEXIS platform via HEAppE. It interfaces with IQM quantum hardware using the :class:`IQMProvider`, :class:`IQMBackend`, and :class:`IQMJob` classes to execute quantum circuits remotely. Aims to provide a general interface for accessing quantum computing resources from multiple vendors to support academic quantum infrastructure access.

**Authors:**
- Jan Swiatkowski (jan.swiatkowski@vsb.cz)
- Jakub Konvička (jakub.konvicka@vsb.cz)
- Jan Martinovič (jan.martinovic@vsb.cz)
Installation
------------

**Prerequisites:**  
The `py4lexis` package (for LEXIS authentication) must be installed first.

**Install py4lexis:**
.. code-block:: bash

   pip3.11 install --index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple py4lexis

**Install QaaS:**
.. code-block:: bash

   pip3.11 install git+https://github.com/It4innovations/quantum-as-a-service.git@main

*For development purposes:*
.. code-block:: bash

   pip3.11 install -e .

**Requirements:**
- Python 3.11
- `pip>=26.0`
- Git
- Valid LEXIS platform credentials
- Access to LEXIS quantum resources

**Recommendations:**
- Use virtual environment (e.g. `venv` or `uv`)

Core Interface
---------------

**QProvider**  
The main entry point for quantum resource access through LEXIS.

.. code-block:: python

   from qaas import QProvider

   provider = QProvider(token, project_name)
   backend = provider.get_backend(resource_name)

**Parameters:**
- ``token`` (str): LEXIS access token
- ``project_name`` (str): LEXIS project identifier
- ``resource_name`` (str): Accounting string (typically ``"EQE1-CZ-P0001"``). Depending of selected resource name a backend for submission will be selected.

**QBackend**  
Represents quantum hardware interface with capabilities for circuit execution.

.. code-block:: python

   # Get hardware specifications
   print(f"Available qubits: {backend.architecture.qubits}")
   print(f"Supported gates: {list(backend.architecture.gates.keys())}")

   # Prepare and execute quantum circuit
   from qiskit import QuantumCircuit
   qc = QuantumCircuit(2, 2)
   qc.h(0)
   qc.cx(0, 1)
   qc.measure_all()

   quantum_circuit = backend.transpile(qc, optimize_single_qubits=False)
   job = backend.run(quantum_circuit, shots=1000)

**QJob**  
Manages quantum job execution and result retrieval.

.. code-block:: python

   job = backend.run(quantum_circuit, shots=1000)
   result = job.result()
   counts = result.get_counts()

**QPulla**  
Experimental pulse-level quantum optimization interface (currently limited to IQM hardware). Provides access to hardware-specific information (chip layout, calibrations) and includes method for submitting quantum pulse sequences (Playlists). The goal is to generalize this interface across quantum hardware backends for hardware-specific circuit compilation and pulse-level optimization.

.. code-block:: python

   # Initialize QPulla
   p = provider.get_pulla()
   client = provider.get_client()
   architecture = client.get_dynamic_architecture(None)

   # Create optimized circuit
   from qiskit import QuantumCircuit
   qc_bell = QuantumCircuit(2, 2)
   qc_bell.h(0)
   qc_bell.cx(0, 1)
   qc_bell.measure_all()

   # Transpile for QPulla
   qc_bell_transpiled = transpile(
       qc_bell,
       backend=pulla_backend,
       layout_method='sabre',
       optimization_level=3
   )
   job = pulla_backend.run(qc_bell_transpiled, shots=100)

.. code-block:: python
   # Initialize QPulla
   p = provider.get_pulla()
   
   # Transpile and optimize
   qc_ghz_transpiled = backend.transpile(
      qc_ghz,
      layout_method='sabre',
      optimization_level=3
   )
   qc_ghz_optimized = optimize_single_qubit_gates(qc_ghz_transpiled)

   # Convert to Pulla
   circuits_ghz, compiler_ghz = qiskit_to_pulla(p, backend, qc_ghz_optimized)

   # Compile
   playlist_ghz, context_ghz = compiler_ghz.compile(circuits_ghz[0])
   settings_ghz, context_ghz = compiler_ghz.build_settings(context_ghz, shots=shots)

   # Execute
   job_ghz = p.submit_playlist(playlist_ghz, settings_ghz, context=context_ghz)
   job_ghz.wait_for_completion()

   # Results
   qiskit_result_ghz = sweep_job_to_qiskit(
      job_ghz,
      shots=shots,
      execution_options=context_ghz['options']
   )

Authentication
---------------

**Automatic token retrieval (recommended):**
.. code-block:: python

   from py4lexis.session import LexisSession
   lexis_session = LexisSession()
   token = lexis_session.get_access_token()

**Manual token specification:**
.. code-block:: python

   token = "your_lexis_access_token"  # Replace with actual token

Basic Usage Example
-------------------

**Complete workflow for a quantum circuit:**

.. code-block:: python

   from py4lexis.session import LexisSession
   from qaas import QProvider
   from qiskit import QuantumCircuit

   # 1. Authentication
   lexis_session = LexisSession()
   token = lexis_session.get_access_token()

   # 2. Configure resources
   LEXIS_PROJECT = "vlq_demo_project"
   LEXIS_RESOURCE_NAME = "qaas_user"

   # 3. Initialize QaaS
   provider = QProvider(token, LEXIS_PROJECT)
   backend = provider.get_backend(LEXIS_RESOURCE_NAME)

   # 4. Create circuit
   qc = QuantumCircuit(2, 2)
   qc.h(0)
   qc.cx(0, 1)
   qc.measure_all()

   # 5. Execute
   qc_transpiled = backend.transpile(qc, optimize_single_qubits=False)
   job = backend.run(qc_transpiled, shots=1000)
   result = job.result()

   # 6. Process results
   counts = result.get_counts()
   for state, count in counts.items():
       if count > 0:
           print(f"State '{state}': {count} counts")

Circuit Transpilation
---------------------

Quantum circuits must be transpiled for IQM hardware before execution:

.. code-block:: python

   qc_transpiled = backend.transpile(
       quantum_circuit,
       optimize_single_qubits=False
   )

**Parameters:**
- ``quantum_circuit``: Qiskit :class:`QuantumCircuit` object
- ``optimize_single_qubits`` (bool): Enable single-qubit gate optimization (default: ``False``)

Hardware Information
---------------------

Access quantum hardware specifications:

.. code-block:: python

   # Available qubits
   print(f"Qubits: {backend.architecture.qubits}")

   # Supported gate set
   print(f"Gates: {list(backend.architecture.gates.keys())}")

Job Execution and Results
-------------------------

**Submit job:**
.. code-block:: python

   job = backend.run(quantum_circuit, shots=1000)

**Retrieve results:**
.. code-block:: python

   result = job.result()
   counts = result.get_counts()

   for bitstring, count in counts.items():
       if count > 0:
           print(f"State |{bitstring}⟩: {count} measurements")

Exception Handling
-------------------

QaaS raises :class:`QException` for errors:

.. code-block:: python

   try:
       provider = QProvider(token, project)
       backend = provider.get_backend(resource)
       job = backend.run(circuit, shots=1000)
       result = job.result()
   except QException as e:
       print(f"QaaS error: {e}")
   except Exception as e:
       print(f"Unexpected error: {e}")

**Common scenarios:**
- LEXIS authentication failures
- Invalid project/resource specifications
- Backend connectivity issues
- Circuit transpilation errors
- Job execution failures

Dependencies
-------------

.. table:: QaaS dependencies
   :widths: 25 15 30

   ==============  ==============  ===============
   Package          Version Range  Purpose
   ==============  ==============  ===============
   qiskit           1.2.4         Quantum circuit framework
   iqm-client[qiskit] 33.0.*     IQM quantum interface
   iqm-exa-common   27.4.*       IQM common utilities
   iqm-station-control-client 12.0.* IQM hardware control
   iqm-data-definitions 2.19     IQM data structures
   iqm-pulla        12.0.*       Pulse-level quantum optimization
   qiskit_aer       >=0.15.0,<1.0.0 Quantum circuit simulation
   Py4HEAppE        >=2.5.0      HEAppE job management
   cryptography     >=43.0.0     Cryptographic library
   bcrypt           >=4.2.0      Password hashing
   cffi             >=1.17.1     Foreign Function Interface
   click            >=8.1.7      Command-line interface toolkit
   jwcrypto         1.5.*        JSON Web Cryptography
   PyJWT[crypto]    2.10.*      JSON Web Token library


Technical Architecture
----------------------

QaaS implements a 3-layer architecture:

1. **Provider Layer** (`provider.py`)  
   Handles LEXIS authentication and quantum backend access.

2. **Client Layer** (`client.py`)  
   Manages HEAppE job submission and result retrieval.

3. **Backend Implementation** (`backend_iqm.py`)  
   Creates IQM-specific quantum backends with circuit compilation.

**Key features:**
- Pulse-level quantum optimization via QPulla
- Hardware-specific circuit transpilation
- Real-time hardware calibration

Execution Flow
---------------

HEAppE executes jobs through these steps:
1. Job submission via command templates
2. `run_init.sh` sets up environment or return particular system information
3. `run_execution.sh` executes quantum circuit
4. Results returned via HEAppE to QaaS client

Roadmap
--------

- **Q2 2026**: Implement low-level quantum circuit tuning

License
-------

Not specified (to be defined)

Benchmarks
-----------
All measurement datasets from benchmarks are located in the `measurements_data` directory.

**Experiment A: Batch Job Submission**
Measures IQM job execution times with 100, 1000, and 5000 shots (100 repetitions each).
All jobs are submitted in a batch via `.run()`, then results are collected sequentially.

- Jobs: 300 total (100 per shot count)
- Pattern: Submit all → Wait for all results

**Experiment B: Sequential Job Execution**
Measures per-job overhead with immediate result retrieval (50 repetitions per shot count).
Each job is submitted and completed before the next submission.

- Jobs: 150 total (50 per shot count: 100, 1000, 5000 shots)
- Pattern: Submit one → Wait for result → Repeat
- Includes 1s sleep between jobs

**Experiment C: QaaS System Benchmark**

To accomplish the target of QaaS client optimization, measurements were performed. In case of experiment C there was 150 execution executed with 500 shots where .run method was called immediately 150 times and then code waits for all jobs to finish in threads.

**Experiment D: QaaS System Benchmark**

In case of experiment D, the experiment consists of executing the same circuit 150 times and waiting for the results (specifically using the .run() and .result() methods).

**Measurement C and D Insights**

The measurement results revealed that the main optimization opportunities are in the communication paths. These include communication between the IQM system and the QaaS backend service (running under a SLURM job) when the results are ready and need to be transferred, as well as the communication between the end of the SLURM job end and the QaaS client on the user side when retrieving information about job completion.
Additional Notes

After measurement was found that SLURM has ability to measure in resolution of seconds only. Thus it is not suitable for precise overhead analysis.
Event Intervals

    IQM Queue Wait: Time interval between 'iqm_job_pending_execution', 'iqm_job_execution_started' events.
    Comm: IQM→Backend: Time interval between 'iqm_job_completed', 'iqm_client_job_ended' events.
    Backend: IQM→Postprocess Gap: Time interval between 'qaas_backend_run_postprocessing_started','iqm_client_job_ended' events. As consequence this includes posprocessing on VM on QAAS backend, transfer of data to client and also idle before the client.


**Experiment E I.: QaaS System Benchmark**
In total there was 216 executions, 12 circuits were utilized.
Circuits were randomly generated with following parameters using qiskit.random_circuit:
        [6, 12, 24], # qubits
        [5,25,55,100],  # depth
        [1000,2500,5000], # number of shots
Circuits were executed in order by circuit size in qubits and its depth. Before each execution was waited for results.

**Experiment E II.: QaaS System Benchmark**
In total there was 216 executions, 12 circuits were utilized.
Circuits were randomly generated with following parameters using qiskit.random_circuit:
        [6, 12, 24], # qubits
        [5,25,55,100],  # depth
        [1000,2500,5000], # number of shots
Circuits were executed out of order. After circuits were randomly generated, they were shuffled. Before each execution was waited for results.

**Experiment E III.: QaaS System Benchmark**
Circuits were randomly generated with following parameters using qiskit.random_circuit:
        [6, 12, 24], # qubits
        [5,25,55,100],  # depth
        [1000,2500,5000], # number of shots
Circuits were executed out of order. After circuits were randomly generated, they were shuffled. Executions were submitted without waiting for results. After all were submitted, results were awaited.

**Experiment F I.: QaaS System Benchmark**
Multiple circuits were used with combined sizes with varying depths and qubits counts and different shots. They were executed in batches ranging from 2^1 to 2^10. Batch size was incremented by doubling each time, and repeating each batch size execution 15 times. There should be max 6 different circuits.
In total there was 1380 executions.

**Experiment F II.: QaaS System Benchmark**
Single circuit was used and executed in batches from 2^1 to 2^10. Similarly, double the batch size for each subsequent run, repeating each batch size execution 15 times.
In total there was 1270 executions.