"""Metadata about LEXIS projects, resources and the quantum clusters they grant access to.

Hierarchy:
    LexisProject
      └─ LexisResource            (one LEXIS ProjectResource, LocationTypeId == 7)
           └─ QCluster  (1..n)    (one assignment == one HEAppE cluster == one backend)
                └─ QMachineInfo   (/heappe/QScheduler/MachineInfo for that cluster)

QBackendMetadata is a read-only view over (project, resource, cluster) and keeps the
public property names of the previous single-backend implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iqm.station_control.interface.models import (
        DynamicArchitecture,
        StaticArchitecture,
    )
# sw_stack -> (supplier, quantum technology)
_SW_STACK_TRAITS: dict[str, tuple[str, str]] = {
    "iqm": ("IQM", "SUPERCONDUCTING"),
}


class LexisProject:
    """Information about LEXIS Project entity.

    Attributes:
        project_name (str): The name of the project.
        project_validity_start (str): The start date of the project validity.
        project_validity_end (str): The end date of the project validity.
    """

    def __init__(
        self,
        project_name: str,
        project_validity_start: str = "-",
        project_validity_end: str = "-",
    ):
        """Initialize a new LexisProject instance.

        Args:
            project_name (str): The name of the project.
            project_validity_start (str, optional): The start date of the project validity. Defaults to "-".
            project_validity_end (str, optional): The end date of the project validity. Defaults to "-".
        """
        self._project_name = project_name
        self._project_validity_start = project_validity_start
        self._project_validity_end = project_validity_end

    @property
    def project_name(self) -> str:
        """Get the name of the project.

        Returns:
            str: The name of the project.
        """
        return self._project_name

    @property
    def project_validity_start(self) -> str:
        """Get the start date of the project validity.

        Returns:
            str: The start date of the project validity.
        """
        return self._project_validity_start

    @property
    def project_validity_end(self) -> str:
        """Get the end date of the project validity.

        Returns:
            str: The end date of the project validity.
        """
        return self._project_validity_end


@dataclass
class QMachineInfo:
    """Parsed response of /heappe/QScheduler/MachineInfo.

    Attributes:
        sw_stack (str | None): The software stack identifier.
        qscheduler_version (str | None): The version of QScheduler.
        qccsw_version (str | None): The version of QCCSW.
        server_version (str | None): The version of the server.
        station_control_version (str | None): The version of Station Control.
        supplier (str): The supplier of the quantum technology.
        quantum_technology (str): The type of quantum technology.
        raw (dict[str, Any]): The raw response payload.
    """

    sw_stack: str | None = None
    qscheduler_version: str | None = None
    qccsw_version: str | None = None
    server_version: str | None = None
    station_control_version: str | None = None
    supplier: str = "UNKNOWN"
    quantum_technology: str = "UNKNOWN"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> QMachineInfo:
        """Create a QMachineInfo instance from a response payload.

        Args:
            payload (dict[str, Any]): The response payload.

        Returns:
            QMachineInfo: A new QMachineInfo instance.
        """
        sw_stack = payload.get("type")
        version = payload.get("version") or {}
        supplier, technology = _SW_STACK_TRAITS.get(sw_stack, ("UNKNOWN", "UNKNOWN"))
        return cls(
            sw_stack=sw_stack,
            qscheduler_version=version.get("qscheduler_version"),
            qccsw_version=version.get("qccsw_version"),
            server_version=version.get("server_version"),
            station_control_version=version.get("station_control_version"),
            supplier=supplier,
            quantum_technology=technology,
            raw=payload,
        )


@dataclass
class QCluster:
    """One quantum cluster (LEXIS assignment / HEAppE cluster) within a LexisResource.

    Attributes:
        name (str): The name of the cluster.
        heappe_url (str): The URL of the HEAppE cluster.
        allocated_units (int | None): The number of allocated units.
        aggregation_name (str | None): The name of the aggregation.
        host_supercomputer (str): The name of the host supercomputer.
        cluster_id (int | None): The ID of the cluster in HEAppE.
        cluster_node_type_id (int | None): The node type ID of the cluster in HEAppE.
        machine_info (QMachineInfo | None): The machine information.
        available (bool): Indicates if the cluster is available.
        heappe_project_id (int | None): The project ID in HEAppE.
        error (str | None): The error message if the cluster is not available.
    """

    name: str
    heappe_url: str
    allocated_units: int | None = None
    aggregation_name: str | None = None
    host_supercomputer: str = "-"

    # Resolved from HEAppE after authorization
    cluster_id: int | None = None
    cluster_node_type_id: int | None = None
    machine_info: QMachineInfo | None = None
    available: bool = False
    heappe_project_id: int | None = (
        None  # project ID in HEAppE (used for job submission), identifier of LEXIS Project in HEAppE
    )
    error: str | None = None  # reason why the cluster is not available


class LexisResource:
    """Information about LEXIS Resource entity and the quantum clusters assigned to it.

    Attributes:
        resource_name (str): The name of the resource.
        project_resource_id (str): The ID of the resource assignment in LEXIS.
        resource_validity_start (str): The start date of the resource validity.
        resource_validity_end (str): The end date of the resource validity.
        clusters (dict[str, QCluster]): A dictionary of clusters assigned to the resource.
    """

    def __init__(
        self,
        resource_name: str,
        project_resource_id: str,
        resource_validity_start: str,
        resource_validity_end: str,
        clusters: dict[str, QCluster] | None = None,
    ):
        """Initialize a new LexisResource instance.

        Args:
            resource_name (str): The name of the resource.
            project_resource_id (str): The ID of the resource assignment in LEXIS.
            resource_validity_start (str): The start date of the resource validity.
            resource_validity_end (str): The end date of the resource validity.
            clusters (dict[str, QCluster] | None, optional): A dictionary of clusters assigned to the resource. Defaults to None.
        """
        self._resource_name = resource_name
        self._project_resource_id = project_resource_id
        self._resource_validity_start = resource_validity_start
        self._resource_validity_end = resource_validity_end
        self._clusters: dict[str, QCluster] = dict(clusters or {})

    @property
    def resource_name(self) -> str:
        """Get the name of the resource.

        Returns:
            str: The name of the resource.
        """
        return self._resource_name

    @property
    def project_resource_id(self) -> str:
        """Get the ID of the resource assignment in LEXIS.

        Returns:
            str: The ID of the resource assignment in LEXIS.
        """
        return self._project_resource_id

    @property
    def resource_validity_start(self) -> str:
        """Get the start date of the resource validity.

        Returns:
            str: The start date of the resource validity.
        """
        return self._resource_validity_start

    @property
    def resource_validity_end(self) -> str:
        """Get the end date of the resource validity.

        Returns:
            str: The end date of the resource validity.
        """
        return self._resource_validity_end

    @property
    def clusters(self) -> dict[str, QCluster]:
        """Get the clusters assigned to the resource.

        Returns:
            dict[str, QCluster]: A dictionary of clusters assigned to the resource.
        """
        return self._clusters

    @property
    def cluster_names(self) -> list[str]:
        """Get the names of the clusters assigned to the resource.

        Returns:
            list[str]: A list of cluster names.
        """
        return sorted(self._clusters)

    @property
    def allocated_units(self) -> int:
        """Get the sum of allocations over all clusters of the resource.

        Returns:
            int: The sum of allocations.
        """
        return sum(c.allocated_units or 0 for c in self._clusters.values())

    def get_cluster(self, name: str) -> QCluster:
        """Get a specific cluster by name.

        Args:
            name (str): The name of the cluster.

        Returns:
            QCluster: The cluster with the specified name.

        Raises:
            KeyError: If the cluster is not found.
        """
        try:
            return self._clusters[name]
        except KeyError:
            raise KeyError(
                f"Cluster '{name}' not found in resource '{self._resource_name}'. "
                f"Available: {self.cluster_names}"
            ) from None


class QBackendMetadata:
    """Read-only view of one quantum backend (= one QCluster of a LexisResource).

    Attributes:
        lexis_project (LexisProject): The LexisProject instance.
        lexis_resource (LexisResource): The LexisResource instance.
        cluster (QCluster): The QCluster instance.
        static_architecture (StaticArchitecture | None): The static architecture of the backend.
        dynamic_architecture (DynamicArchitecture | None): The dynamic architecture of the backend.
    """

    def __init__(
        self,
        lexis_project: LexisProject,
        lexis_resource: LexisResource,
        cluster: QCluster,
        static_architecture: StaticArchitecture | None = None,
        dynamic_architecture: DynamicArchitecture | None = None,
    ):
        """Initialize a new QBackendMetadata instance.

        Args:
            lexis_project (LexisProject): The LexisProject instance.
            lexis_resource (LexisResource): The LexisResource instance.
            cluster (QCluster): The QCluster instance.
            static_architecture (StaticArchitecture | None, optional): The static architecture of the backend. Defaults to None.
            dynamic_architecture (DynamicArchitecture | None, optional): The dynamic architecture of the backend. Defaults to None.
        """
        self._lexis_project = lexis_project
        self._lexis_resource = lexis_resource
        self._cluster = cluster
        self._static_architecture = static_architecture
        self._dynamic_architecture = dynamic_architecture

    def __repr__(self) -> str:
        """Return a string representation of the QBackendMetadata instance.

        Returns:
            str: A string representation of the QBackendMetadata instance.
        """
        return (
            f"QBackendMetadata(backend_name={self.backend_name!r}, "
            f"available={self.available}, sw_stack={self.software_stack!r}, "
            f"qaas_version={self.qaas_version!r})"
        )

    # --- structure ---------------------------------------------------------

    @property
    def cluster(self) -> QCluster:
        """Get the QCluster instance.

        Returns:
            QCluster: The QCluster instance.
        """
        return self._cluster

    @property
    def lexis_project(self) -> LexisProject:
        """Get the LexisProject instance.

        Returns:
            LexisProject: The LexisProject instance.
        """
        return self._lexis_project

    @property
    def lexis_resource(self) -> LexisResource:
        """Get the LexisResource instance.

        Returns:
            LexisResource: The LexisResource instance.
        """
        return self._lexis_resource

    @property
    def machine_info(self) -> QMachineInfo | None:
        """Get the machine information of the cluster.

        Returns:
            QMachineInfo | None: The machine information of the cluster.
        """
        return self._cluster.machine_info

    # --- cluster-level -----------------------------------------------------

    @property
    def backend_name(self) -> str:
        """Get the name of the quantum backend.

        Returns:
            str: The name of the quantum backend.
        """
        return self._cluster.name

    @property
    def host_entity(self) -> str:
        """Get the name of the host entity.

        Returns:
            str: The name of the host entity.
        """
        return self._cluster.name

    @property
    def heappe_url(self) -> str:
        """Get the URL of the HEAppE cluster.

        Returns:
            str: The URL of the HEAppE cluster.
        """
        return self._cluster.heappe_url

    @property
    def cluster_id(self) -> int | None:
        """Get the ID of the cluster in HEAppE.

        Returns:
            int | None: The ID of the cluster in HEAppE.
        """
        return self._cluster.cluster_id

    @property
    def cluster_node_type_id(self) -> int | None:
        """Get the node type ID of the cluster in HEAppE.

        Returns:
            int | None: The node type ID of the cluster in HEAppE.
        """
        return self._cluster.cluster_node_type_id

    @property
    def allocated_units(self) -> int | None:
        """Get the number of allocated units.

        Returns:
            int | None: The number of allocated units.
        """
        return self._cluster.allocated_units

    @property
    def host_supercomputer(self) -> str:
        """Get the name of the host supercomputer.

        Returns:
            str: The name of the host supercomputer.
        """
        return self._cluster.host_supercomputer

    @property
    def available(self) -> bool:
        """Check if the cluster is available.

        Returns:
            bool: True if the cluster is available, False otherwise.
        """
        return self._cluster.available

    @property
    def error(self) -> str | None:
        """Get the error message if the cluster is not available.

        Returns:
            str | None: The error message if the cluster is not available.
        """
        return self._cluster.error

    # --- machine info ------------------------------------------------------

    @property
    def qaas_version(self) -> str | None:
        """Get the version of deployed QaaS backend API (QScheduler).

        Returns:
            str | None: The version of deployed QaaS backend API (QScheduler).
        """
        mi = self._cluster.machine_info
        return mi.qscheduler_version if mi else None

    @property
    def qccsw_version(self) -> str | None:
        """Get the version of QCCSW.

        Returns:
            str | None: The version of QCCSW.
        """
        mi = self._cluster.machine_info
        return mi.qccsw_version if mi else None

    @property
    def software_stack(self) -> str | None:
        """Get the software stack identifier.

        Returns:
            str | None: The software stack identifier.
        """
        mi = self._cluster.machine_info
        return mi.sw_stack if mi else None

    @property
    def supplier(self) -> str:
        """Get the supplier of the quantum technology.

        Returns:
            str: The supplier of the quantum technology.
        """
        mi = self._cluster.machine_info
        return mi.supplier if mi else "-"

    @property
    def quantum_technology(self) -> str:
        """Get the type of quantum technology.

        Returns:
            str: The type of quantum technology.
        """
        mi = self._cluster.machine_info
        return mi.quantum_technology if mi else "-"

    # --- dynamic architecture ------------------------------------------------
    @property
    def dynamic_architecture(self) -> Any | None:
        """Return the dynamic architecture of the backend, if available.

        Returns:
            Any | None: The dynamic architecture of the backend.
        """
        return self._dynamic_architecture

    @property
    def static_architecture(self) -> Any | None:
        """Return the static architecture of the backend, if available.

        Returns:
            Any | None: The static architecture of the backend.
        """
        return self._static_architecture
