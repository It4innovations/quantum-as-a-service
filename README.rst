QaaS — Quantum-as-a-Service
===========================

.. image:: https://img.shields.io/badge/docs-GitHub%20Pages-blue
   :target: https://it4innovations.github.io/quantum-as-a-service/
   :alt: Documentation

Access IQM quantum hardware through the `LEXIS platform <https://lexis-project.eu/>`_ via HEAppE.

📖 **Full documentation:** https://it4innovations.github.io/quantum-as-a-service/

Quick start
-----------

.. code-block:: bash

   pip3.11 install git+https://github.com/It4innovations/quantum-as-a-service.git@main

.. code-block:: python

   from py4lexis.session import LexisSession
   from qaas import QProvider
   from qiskit import QuantumCircuit

   token    = LexisSession().get_access_token()
   backend  = QProvider(token, "my_project").get_backend("EQE1-CZ-P0001")

   qc = QuantumCircuit(2, 2)
   qc.h(0); qc.cx(0, 1); qc.measure_all()

   counts = backend.run(backend.transpile(qc), shots=1000).result().get_counts()
   print(counts)

Authors
-------

- Jan Swiatkowski (jan.swiatkowski@vsb.cz)
- Jakub Konvička (jakub.konvicka@vsb.cz)
- Jan Martinovič (jan.martinovic@vsb.cz)
- Ladislav Foltyn (ladislav.foltyn@vsb.cz)

License
-------

Apache 2.0 — see ``LICENSE``.
