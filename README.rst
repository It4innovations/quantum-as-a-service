QaaS — Quantum-as-a-Service
===========================

.. image:: https://img.shields.io/badge/docs-GitHub%20Pages-blue
   :target: https://it4innovations.github.io/quantum-as-a-service/
   :alt: Documentation

Access IQM quantum hardware through the `LEXIS platform <https://lexis-project.eu/>`_ via `HEAppE <https://heappe.eu/>`_.

**Documentation:** https://it4innovations.github.io/quantum-as-a-service/

Requirements
------------

- Python 3.11+
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
