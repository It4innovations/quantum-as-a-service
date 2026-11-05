"""Pulse-level access (IQM Pulla) to IQM machines behind HEAppE/QScheduler.

:class:`QPulla` is an :class:`iqm.iqm_client.IQMClient` whose executor and storage go
through HEAppE (see :mod:`.heappe_iqm`), so IQM's pulse-level API works as documented
by IQM, only the connection differs::

    from qheappe_client import QProvider

    provider = QProvider(lexis_project="my_project")
    pulla = provider.get_pulla("my_resource", "VLQ")

    # Circuits -> pulse-level job, with IQM's compiler
    circuits, compiler = pulla.qiskit_to_iqm(pulla.backend.transpile(qiskit_circuits))
    settings = compiler.get_settings(circuits=circuits)
    settings.set_shots(1000)
    run_definition, context = compiler.compile(circuits, settings=settings)

    job = pulla.submit_playlist(run_definition, context=context)
    job.wait_for_completion()
    result = pulla.sweep_job_to_qiskit(job, shots=1000)  # qiskit Result

Requirements:

* IQM 36 stack (``qaas[iqm_qiskit]``: iqm-client 36.x, iqm-pulla 15.x);
* QScheduler v0.2.2+ with pulse access enabled for the machine;
* HEAppE 6.7.0+ (forwards the task ``Kind``, proxies machine artifacts and
  whole calibration sets).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import cached_property
from typing import TYPE_CHECKING, Any

from iqm.iqm_client import IQMClient
from iqm.qiskit_iqm import pulse_level_utils
from iqm.station_control.client.job_trackers import RunJobTracker
from qiskit import QuantumCircuit
from qiskit.result import Result

from .heappe_iqm import HEAppEExecutor, HEAppEStorage

if TYPE_CHECKING:
    from iqm.cpc.compiler.compiler import Compiler
    from iqm.cpc.core.observation.observation_loading_rules import RuleType
    from iqm.pulse import Circuit
    from iqm.pulse.quantum_ops import QuantumOp

    from ..qclient import QClient
    from ..qiskit.qbackend_iqm import QBackendIQM

log = logging.getLogger(__name__)


class QPulla(IQMClient):
    """IQM pulse-level client (Pulla) for one backend, connected through HEAppE.

    Everything :class:`~iqm.iqm_client.IQMClient` offers for pulse-level work is
    available: :meth:`get_standard_compiler`, :meth:`submit_playlist`,
    :meth:`get_chip_topology`, :meth:`get_calibration_set`,
    :meth:`get_calibration_set_stash`, :meth:`get_dynamic_quantum_architecture`, ...
    plus Qiskit helpers bound to the backend (:meth:`qiskit_to_iqm`,
    :meth:`get_qiskit_compiler`, :meth:`sweep_job_to_qiskit`).

    Usually obtained from :meth:`QProvider.get_pulla` or ``backend.client``.

    Args:
        qclient: Client bound to the target backend (see :meth:`QClient.bind`).
        backend: Qiskit backend of the same machine; built on first use if ``None``.
        walltime_limit_secs: HEAppE walltime limit of each submitted task.
        job_name: HEAppE job name of submitted jobs.
        use_sessions: Run submitted tasks in QScheduler sessions.
    """

    def __init__(
        self,
        qclient: QClient,
        backend: QBackendIQM | None = None,
        *,
        walltime_limit_secs: int = 3600,
        job_name: str = "qpulla_job",
        use_sessions: bool = False,
    ):
        executor = HEAppEExecutor(
            qclient,
            walltime_limit_secs=walltime_limit_secs,
            job_name=job_name,
            use_sessions=use_sessions,
        )
        super().__init__(executor=executor, storage=HEAppEStorage(qclient, executor))
        self._qclient = qclient
        self._backend = backend

    def __repr__(self) -> str:
        return f"{type(self).__name__}(backend={self.quantum_computer_name!r})"

    @property
    def qclient(self) -> QClient:
        """HEAppE client this instance sends its requests through."""
        return self._qclient

    @property
    def executor(self) -> HEAppEExecutor:
        """Executor that submits jobs (submission options live here)."""
        return self._executor

    @property
    def backend(self) -> QBackendIQM:
        """Qiskit backend of the machine, sharing this client."""
        if self._backend is None:
            # Imported here: the factory imports the qiskit backends, which import this module.
            from ..iqm_compat import QBackendIQMFactory

            self._backend = QBackendIQMFactory.create(
                self._qclient, self._qclient.get_quantum_backend_info()
            )
            self._backend.client = self
        return self._backend

    @cached_property
    def quantum_computer_name(self) -> str:
        """Backend (QScheduler machine) name."""
        return self._qclient.get_quantum_backend_info().backend_name

    def get_health(self) -> dict[str, Any]:
        """Not available through HEAppE; see :meth:`get_about` for machine information."""
        raise NotImplementedError("Health status is not available through HEAppE.")

    def get_about(self) -> dict[str, Any]:
        """Machine information reported by QScheduler (``MachineInfo``)."""
        info = self._qclient.get_quantum_backend_info().machine_info
        return dict(info.raw) if info is not None and info.raw else {}

    def _debug_info(self) -> None:
        """Print backend, client and IQM library versions."""
        from importlib import metadata

        versions = {}
        for dist in (
            "qaas",
            "iqm-client",
            "iqm-pulla",
            "iqm-pulse",
            "iqm-station-control-client",
        ):
            try:
                versions[dist] = metadata.version(dist)
            except metadata.PackageNotFoundError:
                versions[dist] = None
        print(
            {
                "backend": self.quantum_computer_name,
                "about": self.get_about(),
                **versions,
            }
        )

    # ---------- Qiskit helpers bound to the backend ----------

    def qiskit_to_iqm(
        self,
        qiskit_circuits: QuantumCircuit | Sequence[QuantumCircuit],
        *,
        loading_rules: list[RuleType] | None = None,
    ) -> tuple[list[Circuit], Compiler]:
        """Convert transpiled Qiskit circuits to IQM circuits and get a compiler for them.

        See :func:`iqm.qiskit_iqm.pulse_level_utils.qiskit_to_iqm`.

        Args:
            qiskit_circuits: Circuits transpiled for :attr:`backend`.
            loading_rules: Observation loading rules; default calibration set if ``None``.

        Returns:
            IQM circuits, compiler with the backend's qubit mapping.
        """
        return pulse_level_utils.qiskit_to_iqm(
            self.backend, qiskit_circuits, loading_rules=loading_rules
        )

    def get_qiskit_compiler(
        self,
        *,
        loading_rules: list[RuleType] | None = None,
        exa_style_pp: bool = True,
        controller_mapping: dict[str, dict[str, str]] | None = None,
        gate_definitions: dict[str, QuantumOp] | None = None,
    ) -> Compiler:
        """Compiler that also transpiles Qiskit circuits for :attr:`backend`.

        See :func:`iqm.qiskit_iqm.pulse_level_utils.get_qiskit_compiler`.
        """
        return pulse_level_utils.get_qiskit_compiler(
            self.backend,
            loading_rules=loading_rules,
            exa_style_pp=exa_style_pp,
            controller_mapping=controller_mapping,
            gate_definitions=gate_definitions,
        )

    @staticmethod
    def sweep_job_to_qiskit(job: RunJobTracker, *, shots: int) -> Result:
        """Results of a completed pulse-level job as a Qiskit ``Result``.

        See :func:`iqm.qiskit_iqm.pulse_level_utils.sweep_job_to_qiskit`.
        """
        return pulse_level_utils.sweep_job_to_qiskit(job, shots=shots)
