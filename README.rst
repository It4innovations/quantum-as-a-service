QaaS — Quantum-as-a-Service
===========================

.. image:: https://img.shields.io/badge/docs-GitHub%20Pages-blue
   :target: https://it4innovations.github.io/quantum-as-a-service/
   :alt: Documentation

Access IQM quantum hardware through the `LEXIS platform <https://lexis-project.eu/>`_ via `HEAppE <https://heappe.eu/>`_.

**Documentation:** https://it4innovations.github.io/quantum-as-a-service/

Requirements
------------

- Python 3.12
- required Python packages: see `dependencies` in `pyproject.toml <pyproject.toml>`_

Quick start
-----------

QaaS is available on `PyPI <https://pypi.org/project/qaas>`_, so you can install it easily using `pip`.

.. code-block:: bash

   pip install qaas

Alternatively, you can install the latest (development) version using:

.. code-block:: bash

   pip install git+https://github.com/It4innovations/quantum-as-a-service.git@main

Example usage:

.. code-block:: python

   from qaas import QProvider
   from qiskit import QuantumCircuit

   backend  = QProvider("my_project").get_backend("EQE1-CZ-P0001")

   qc = QuantumCircuit(2, 2)
   qc.h(0); qc.cx(0, 1); qc.measure_all()

   counts = backend.run(backend.transpile(qc), shots=1000).result().get_counts()
   print(counts)


Also OpenQASM is supported on input:

.. code-block:: python

   from qaas import export_qasm3_with_custom_move as qasm3dumps

   qc = QuantumCircuit(2, 2)
   qc.h(0); qc.cx(0, 1); qc.measure_all()

   qasm_transpiled_qc:str = qasm3dumps(backend.transpile(qc))

   counts = backend.run([qasm_transpiled_qc], shots=1000).result().get_counts()
   print(counts)



IQM Pulla
----------

.. code-block:: python

   from qiskit import QuantumCircuit, visualization
   from qiskit.compiler import transpile
   from iqm.qiskit_iqm.iqm_transpilation import optimize_single_qubit_gates
   from iqm.pulla.utils_qiskit import sweep_job_to_qiskit
   from qaas.client import QProvider, QBackend
   from qaas.client.qpulla import qiskit_to_pulla, QPullaBackendIQM

   SHOTS = 100

   provider = QProvider("my_project")
   client = provider.get_client(lexis_resource_name)
   dqa = client.get_dynamic_architecture()
   compiler = p.get_standard_compiler()
   # Create Pulla instance
   p = provider.get_pulla(lexis_resource_name)
   pulla_backend: QPullaBackendIQM = QPullaBackendIQM(dqa,p,compiler)

   qc_transpiled = backend.transpile(
      qc,
      layout_method='sabre',
      optimization_level=0
   )
   # Optimize single-qubit gates
   qc_optimized = optimize_single_qubit_gates(qc_transpiled)

   circuits, compiler = qiskit_to_pulla(p, pulla_backend, [qc_optimized])
   
   # Build settings for execution
   settings = compiler.get_settings(circuits[0])
   settings.set_shots(SHOTS) # optional
   # Compile to playlist
   run_definition, context = compiler.compile(circuits, settings=settings)
   
   # Submit playlist returns SweepJob
   job = p.submit_playlist(run_definition, context=context)
   job.wait_for_completion()
   
   # Get raw results
   raw_results = job.result()
   
   # Convert to Qiskit result format
   qiskit_result = sweep_job_to_qiskit(
      job,
      shots=SHOTS
   )
   # Qiskit Counts
   counts = qiskit_result.get_counts()

Authors
-------

- Jan Swiatkowski (jan.swiatkowski@vsb.cz)
- Jakub Konvička (jakub.konvicka@vsb.cz)
- Jan Martinovič (jan.martinovic@vsb.cz)
- Ladislav Foltyn (ladislav.foltyn@vsb.cz)

License
-------

Apache 2.0 — see ``LICENSE``.
