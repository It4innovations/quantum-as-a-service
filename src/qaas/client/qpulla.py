"""QPulla
- Class wrapping and handling low-level programming of quantum jobs - based on Pulla from IQM
- Currently supports only IQM

"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import QClient
from copy import deepcopy
import logging
from uuid import UUID, uuid4
import copy
from collections.abc import Sequence

from qiskit import QuantumCircuit
from qiskit.providers import Options

from iqm.pulla.pulla import Pulla
from iqm.pulla.interface import CalibrationSetValues
from iqm.pulla.utils import (
    calset_from_observations,
    extract_readout_controller_result_names,
)
from iqm.pulse.playlist.playlist import Playlist

from exa.common.qcm_data.chip_topology import ChipTopology
from iqm.station_control.interface.models import (
    SweepDefinition,
    DynamicQuantumArchitecture,
)
from iqm.cpc.compiler.compiler import (
    STANDARD_CIRCUIT_EXECUTION_OPTIONS,
    STANDARD_CIRCUIT_EXECUTION_OPTIONS_DICT,
    Compiler,
)
from iqm.cpc.compiler.standard_stages import get_standard_stages
from iqm.cpc.interface.compiler import Circuit, CircuitExecutionOptions
from exa.common.data.setting_node import SettingNode
from iqm.qiskit_iqm.iqm_backend import IQMBackendBase
from iqm.pulla.utils_qiskit import (
    qiskit_circuits_to_pulla,
    sweep_job_to_qiskit,
    DummyJob,
)
from iqm.pulla.utils import calset_to_cal_data_tree

from iqm.pulse.builder import ScheduleBuilder, build_quantum_ops

from py4heappe.heappe_v6.core.models import EnvironmentVariableExt

from .utils import QPullaFetchError
from .backend import QJob
from .backend_iqm import QBackendIQM


logger = logging.getLogger(__name__)

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
        logger.debug(
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
        logger.debug("Get the default calibration set")
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

        # Additional
        self._chip_design_record = chip_design_record
        self._duts = duts

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
        calibration_set_values: CalibrationSetValues | None = None,
        circuit_execution_options: CircuitExecutionOptions | dict | None = None,
    ) -> Compiler:
        """Returns a new instance of the compiler with the default calibration set and standard stages. (Original Pulla method)

        Args:
            calibration_set_values: Calibration set to use. If None, the current calibration set will be used.
            circuit_execution_options: circuit execution options to use for the compiler. If a CircuitExecutionOptions
                object is provided, the compiler use it as is. If a dict is provided, the default values will be
                overridden for the present keys in that dict. If left ``None``, the default options will be used.

        Returns:
            The compiler object.

        """
        if circuit_execution_options is None:
            circuit_execution_options = STANDARD_CIRCUIT_EXECUTION_OPTIONS
        elif isinstance(circuit_execution_options, dict):
            circuit_execution_options = CircuitExecutionOptions(
                **STANDARD_CIRCUIT_EXECUTION_OPTIONS_DICT | circuit_execution_options  # type: ignore
            )
        return Compiler(
            calibration_set_values=calibration_set_values
            or self.fetch_default_calibration_set()[0],
            chip_topology=self.get_chip_topology(),
            channel_properties=self._channel_properties,
            component_channels=self._component_channels,
            component_mapping=None,
            stages=get_standard_stages(),
            options=circuit_execution_options,
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
        playlist: Playlist,
        settings: SettingNode,
        *,
        context: dict[str, Any],
        walltime_limit=7200,
    ) -> "QJob":
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

        sweep = SweepDefinition(
            sweep_id=uuid4(),
            playlist=playlist,
            return_parameters=list(
                extract_readout_controller_result_names(context["readout_mappings"])
            ),
            settings=settings,
            dut_label=self.get_chip_label(),
            sweeps=[],
        )

        job_data = {
            "name": "quantum_run_sweep",
            "walltime_limit": walltime_limit,
            "min_cores": 2,  # NOTE: currently unused
            "max_cores": 2,  # NOTE: currently unused
            "tasks": [{"template_parameter_values": []}],
            # Set environment variables for the job
            "environment_variables": [
                EnvironmentVariableExt(name="Q_COMMAND", value="pulla_submit_playlist")
            ],
        }
        if self._qclient.provider_token:
            job_data["environment_variables"] = EnvironmentVariableExt(
                name="IQM_TOKEN", value=self._qclient.provider_token
            )

        # Submit job using QClient
        heappe_job_id = self._qclient.submit_quantum_job(
            job_data, backend=self.remote_pulla, circuits=sweep, run_options=context
        )

        return QJob(self, heappe_job_id, job_type="pulla")


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
    ) -> DummyJob:
        # Convert Qiskit circuits to Pulla circuits
        pulla_circuits = qiskit_circuits_to_pulla(run_input, self._idx_to_qb)

        # Compile the circuits, build settings and execute
        playlist, context = self.compiler.compile(pulla_circuits)
        settings, context = self.compiler.build_settings(context, shots=shots)

        # submit the playlist for execution
        job = self.pulla.submit_playlist(
            playlist, settings, context=copy.deepcopy(context)
        )
        # wait for the job to finish, no timeout (user can use Ctrl-C to stop)
        # TODO it would be better if we did not wait and instead returned a Qiskit JobV1 containing
        # a SweepJob that can be used to actually track the job.
        job.result(timeout_secs=0.0)

        # TODO: on remote
        # Convert the response data to a Qiskit result
        qiskit_result = sweep_job_to_qiskit(
            job.remote_job, shots=shots, execution_options=context["options"]
        )

        # Return a dummy job object that can be used to retrieve the result
        dummy_job = DummyJob(self, qiskit_result)
        return dummy_job

    @classmethod
    def _default_options(cls) -> Options:
        return Options()

    @property
    def max_circuits(self) -> int | None:
        return None


def qiskit_to_pulla(
    pulla: QPulla,
    pulla_backend: QPullaBackendIQM,
    qiskit_circuits: QuantumCircuit | Sequence[QuantumCircuit],
) -> tuple[list[Circuit], Compiler]:
    """Convert transpiled Qiskit quantum circuits to IQM Pulse quantum circuits.

    Also provides the Compiler object for compiling them, with the correct
    calibration set and component mapping initialized.

    Args:
        pulla: Quantum computer pulse level access object.
        qiskit_circuits: One or many transpiled Qiskit QuantumCircuits to convert.

    Returns:
        Equivalent IQM Pulse circuit(s), compiler for compiling them.

    """

    dynamic_arch: DynamicQuantumArchitecture = pulla._qclient.get_dynamic_architecture()
    _calibration_set_id = dynamic_arch.calibration_set_id

    # create a compiler containing all the required station information
    compiler = pulla.get_standard_compiler(
        calibration_set_values=pulla.fetch_calibration_set_values_by_id(
            _calibration_set_id
        ),
    )

    qiskit_circuits = (
        qiskit_circuits if isinstance(qiskit_circuits, list) else [qiskit_circuits]
    )

    # We can be certain run_request contains only Circuit objects, because we created it
    # right in this method with qiskit.QuantumCircuit objects
    circuits: list[Circuit] = [
        qiskit_circuits_to_pulla(c, pulla_backend._idx_to_qb) for c in qiskit_circuits
    ]
    return circuits, compiler
