"""QPulla
- Class wrapping and handling low-level programming of quantum jobs - based on Pulla from IQM
- Currently supports only IQM

"""

import os
import sys

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .client import QClient
from copy import deepcopy
import logging
from uuid import UUID
import copy
from collections.abc import Sequence

from qiskit import QuantumCircuit
from qiskit.providers import Options

from iqm.pulla.pulla import Pulla, PullaStash
from iqm.pulla.interface import CalibrationSetValues
from iqm.pulla.utils import (
    calset_from_observations,
    extract_readout_controller_result_names,
)

from iqm.iqm_server_client.iqm_server_client import StrUUIDOrDefault

from iqm.pulse.playlist.playlist import Playlist

from exa.common.qcm_data.chip_topology import ChipTopology
from exa.common.errors.station_control_errors import NotFoundError
from iqm.station_control.interface.models import (
    SweepDefinition,
    DynamicQuantumArchitecture,
)
from iqm.cpc.compiler.compiler import (
    Compiler,
)
from iqm.cpc.compiler.standard_stages import (
    _STANDARD_CIRCUIT_STAGES,
    _STANDARD_FINAL_STAGES,
    _STANDARD_PULSE_STAGES,
)

from iqm.cpc.interface.circuit_execution import Circuit
from iqm.cpc.compiler.post_process import (
    _STANDARD_POST_PROCESSING_STAGES,
    _STANDARD_CIRCUIT_POST_PROCESSING_STAGES,
)
from iqm.cpc.core.observation.observation_loading_rules import LatestFromStash, RuleType
from exa.common.data.setting_node import SettingNode
from iqm.qiskit_iqm.iqm_backend import IQMBackendBase
from iqm.pulla.utils_qiskit import qiskit_circuits_to_pulla, sweep_job_to_qiskit
from iqm.pulla.utils import calset_to_cal_data_tree
from iqm.station_control.interface.models import RunDefinition

from iqm.pulse.quantum_ops import QuantumOp
from iqm.pulse.builder import ScheduleBuilder, build_quantum_ops

from py4heappe.heappe_v6.core.models import EnvironmentVariableExt

from .utils import QPullaFetchError
from .backend import QJob, QPullaJob
from .backend_iqm import QBackendIQM


log = logging.getLoggerClass()(
    __name__, os.environ.get("QPROVIDER_LOGLEVEL", "INFO").upper()
)

# Formatter for consistent output
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# Decide handler: file or stderr
logfile = os.environ.get("QPROVIDER_LOGFILE")
if logfile:
    handler = logging.FileHandler(logfile, mode="a")
else:
    handler = logging.StreamHandler(sys.stderr)

handler.setFormatter(formatter)
log.addHandler(handler)

CalibrationDataFetchException = RuntimeError


class CalibrationDataProvider:
    """Access calibration info via IQM Server and cache data in memory."""

    def __init__(self, client: "QClient", calibration_sets=None):

        self._qclient = client
        self._calibration_sets: dict[UUID, CalibrationSetValues] = (
            {} if not calibration_sets else copy.deepcopy(calibration_sets)
        )

    def get_calibration_set_values(
        self, calibration_set_id: UUID
    ) -> CalibrationSetValues:
        """Get the calibration set contents from the database and cache it."""
        log.debug(
            "Get the calibration set from the database: cal_set_id=%s",
            calibration_set_id,
        )
        try:
            if calibration_set_id not in self._calibration_sets:
                self._calibration_sets[calibration_set_id] = calset_from_observations(
                    self._qclient.get_calibration_set(calibration_set_id).observations
                )
            return deepcopy(self._calibration_sets[calibration_set_id])
        except Exception as e:
            raise CalibrationDataFetchException(
                "Could not fetch calibration set from the database."
            ) from e

    def get_default_calibration_set(self) -> tuple[CalibrationSetValues, UUID]:
        """Get the default calibration set id from the database, return it and the set contents."""
        log.debug("Get the default calibration set")
        try:
            default_calibration_set = self._qclient.get_calibration_set(None)
            default_calibration_set_values = calset_from_observations(
                default_calibration_set.observations
            )
        except Exception as e:
            raise CalibrationDataFetchException(
                f"Could not fetch default calibration set id from the database: {e}"
            ) from e
        return (
            default_calibration_set_values,
            default_calibration_set.observation_set_id,
        )


class QPulla:
    def __init__(
        self,
        qclient: "QClient",
        remote_pulla: Pulla,
        qbackend: QBackendIQM,
        calibration_sets,
        station_control_settings,
        chip_label,
        channel_properties,
        component_channels,
        chip_design_record,
        duts,
    ):

        self._qclient: "QClient" = qclient

        self._calibration_data_provider: CalibrationDataProvider = (
            CalibrationDataProvider(self._qclient, calibration_sets)
        )
        self._station_control_settings = station_control_settings
        self._chip_label = chip_label
        self._channel_properties = channel_properties
        self._component_channels = component_channels

        self.remote_pulla = remote_pulla

        # Additional - for compiler
        self._chip_design_record = chip_design_record
        self._duts = duts
        self._software_version_set_id = 0

        self._qbackend: QBackendIQM = qbackend
        self._qpulla_backend: "QPullaBackendIQM" = self.get_new_qpulla_backend()

    def get_new_qpulla_backend(self) -> "QPullaBackendIQM":
        """Creates and returns a new QPullaBackendIQM instance.

        This method queries the dynamic architecture from the Q client, gets the
        standard compiler, and constructs a fresh backend using those values.

        Returns:
            QPullaBackendIQM: A newly created backend instance.
        """
        dqa = self._qclient.get_dynamic_architecture()
        compiler = self.get_standard_compiler()
        qpulla_backend: "QPullaBackendIQM" = QPullaBackendIQM(dqa, self, compiler)
        return qpulla_backend

    def get_qpulla_backend(self) -> "QPullaBackendIQM":
        """Returns the cached QPullaBackendIQM instance.

        Returns:
            QPullaBackendIQM: The backend stored on self._qpulla_backend.
        """
        return self._qpulla_backend

    def get_qbackend(self) -> QBackendIQM:
        """Returns the cached QBackendIQM instance.

        Returns:
            QPullaBackendIQM: The backend stored on self._qpulla_backend.
        """
        return self._qbackend

    def get_chip_label(self) -> str:
        if len(self._duts) != 1:
            raise QPullaFetchError(
                f"Expected exactly one chip label, but got {len(self._duts)}"
            )
        return self._duts[0].label

    def get_chip_topology(self) -> ChipTopology:
        return ChipTopology.from_chip_design_record(self._chip_design_record)

    def get_schedule_builder(self) -> ScheduleBuilder:
        """Returns a new instance of ScheduleBuilder
        Returns:
            The ScheduleBuilder object.

        """
        return ScheduleBuilder(
            op_table=build_quantum_ops({}),
            calibration=calset_to_cal_data_tree(
                self.fetch_default_calibration_set()[0]
            ),
            chip_topology=self.get_chip_topology(),
            channels=self._channel_properties,
            component_channels=self._component_channels,
        )

    def get_standard_compiler(
        self,
        loading_rules: list[RuleType] | None = None,
        *,
        exa_style_pp: bool = True,
        controller_mapping: dict[str, dict[str, str]] | None = None,
        gate_definitions: dict[str, QuantumOp] | None = None,
    ) -> Compiler:
        """Returns a new instance of the compiler with the default calibration set and standard stages. (Original Pulla method)

        Args:
            calibration_set_values: Calibration set to use. If None, the current calibration set will be used.
            circuit_execution_options: circuit execution options to use for the compiler. If a CompilerOptions
                object is provided, the compiler use it as is. If a dict is provided, the default values will be
                overridden for the present keys in that dict. If left ``None``, the default options will be used.

        Returns:
            The compiler object.

        """
        pp_stages = (
            deepcopy(_STANDARD_POST_PROCESSING_STAGES)
            if exa_style_pp
            else deepcopy(_STANDARD_CIRCUIT_POST_PROCESSING_STAGES)
        )
        loading_rules = (
            loading_rules
            if loading_rules is not None
            else [LatestFromStash(self.get_calibration_stash())]
        )

        return Compiler(
            dut_label=self.get_chip_label(),
            loading_rules=loading_rules,  # type:ignore[arg-type]
            chip_topology=self.get_chip_topology(),
            software_version_set_id=self._software_version_set_id,
            station_control_settings=self._station_control_settings.model_copy(),
            component_mapping=None,
            controller_mapping=controller_mapping,
            gate_definitions=gate_definitions,
            circuit_stages=deepcopy(_STANDARD_CIRCUIT_STAGES),
            pulse_stages=deepcopy(_STANDARD_PULSE_STAGES),
            final_stages=deepcopy(_STANDARD_FINAL_STAGES),
            pp_stages=pp_stages,
        )

    def get_calibration_stash(
        self, calibration_set_id: StrUUIDOrDefault = "default"
    ) -> PullaStash:
        """Contents of a calibration set as a stash object."""
        try:
            calibration_set_observations = self._qclient.get_calibration_set(
                calibration_set_id
            ).observations
        except NotFoundError:
            if calibration_set_id == "default":
                log.warning(
                    "No default calibration set available. Will initialize an empty PullaStash."
                )
            else:
                warn = f"Calibration set with id={calibration_set_id} not found. Will initialize an empty PullaStash."
                log.warning(warn)
            calibration_set_observations = []
        return PullaStash(
            {
                observation.dut_field: observation
                for observation in calibration_set_observations
            }
        )

    def fetch_default_calibration_set(self) -> tuple[CalibrationSetValues, UUID]:
        """Fetch the default calibration set from the server, in a minimal format.

        Returns:
            Calibration set contents, calibration set ID.

        """
        default_calibration_set, default_calibration_set_id = (
            self._calibration_data_provider.get_default_calibration_set()
        )
        return default_calibration_set, default_calibration_set_id

    def fetch_calibration_set_values_by_id(
        self, calibration_set_id: UUID
    ) -> CalibrationSetValues:
        """Fetch a specific calibration set from the server.

        All calibration sets are cached in-memory, so if the calibration set with the given
        id has already been fetched, it will be returned immediately.

        Args:
            calibration_set_id: ID of the calibration set to fetch.

        Returns:
            Calibration set contents.

        """
        calibration_set = self._calibration_data_provider.get_calibration_set_values(
            calibration_set_id
        )
        return calibration_set

    def submit_playlist(
        self,
        playlist: RunDefinition,
        *,
        context: dict[str, Any],
        walltime_limit=7200,
    ) -> "QPullaJob":
        """Submit a Playlist of instruction schedules for execution on the remote quantum computer.

        :param playlist: Schedules to execute.
        :param settings: Station settings to be used for the execution.
        :param context: Context object of the compiler run that produced ``playlist``, containing the readout mappings.
            Required for postprocessing the results.
        :param walltime_limit: Maximum time, until execution times out.

        :returns:
            Created job object, used to query the job status and the execution results.

        """
        readout_components = []
        for _, channel in self._component_channels.items():
            for k, v in channel.items():
                if k == "readout":
                    readout_components.append(v)

        job_data = {
            "name": "quantum_run_sweep",
            "walltime_limit": walltime_limit,
            "min_cores": 2,  # NOTE: currently unused
            "max_cores": 2,  # NOTE: currently unused
            "tasks": [{"template_parameter_values": []}],
            # Set environment variables for the job
            "environment_variables": [],
        }
        if self._qclient.provider_token:
            job_data["environment_variables"] = EnvironmentVariableExt(
                name="IQM_TOKEN", value=self._qclient.provider_token
            )

        log.debug("playlist - context: %s\n", str(context))

        # Submit job using QClient
        heappe_job_id = self._qclient.submit_quantum_job(
            job_data,
            backend=self.remote_pulla,
            circuits=deepcopy(playlist),
            run_options=deepcopy(context),
        )

        # # Get shots from settings
        # controller_settings = (
        #     settings["controllers"] if "controllers" in settings.children else settings
        # )

        return QPullaJob(1, self._qbackend, heappe_job_id)


class QPullaBackendIQM(QBackendIQM, IQMBackendBase):
    """A backend that compiles circuits locally using Pulla and submits them to Station Control for execution.

    Args:
        architecture: Describes the backend architecture.
        pulla: Instance of Pulla used to execute the circuits.
        compiler: Instance of Compiler used to compile the circuits.

    """

    def __init__(
        self,
        architecture: DynamicQuantumArchitecture,
        pulla: QPulla,
        compiler: Compiler,
    ):
        IQMBackendBase.__init__(self, architecture, name="IQMPullaBackend")
        self.pulla = pulla
        self.compiler = compiler

    def run(
        self,
        run_input: QuantumCircuit | list[QuantumCircuit],
        shots: int = 1024,
        **options,
    ) -> QPullaJob:
        # Convert Qiskit circuits to Pulla circuits
        pulla_circuits = qiskit_circuits_to_pulla(run_input, self._idx_to_qb)

        # Compile the circuits, build settings and execute
        playlist, context = self.compiler.compile(pulla_circuits)
        settings, context = self.compiler.build_settings(context, shots=shots)

        # submit the playlist for execution
        job = self.pulla.submit_playlist(
            playlist, settings, context=copy.deepcopy(context)
        )
        return job

    @classmethod
    def _default_options(cls) -> Options:
        return Options()

    @property
    def max_circuits(self) -> int | None:
        return None
