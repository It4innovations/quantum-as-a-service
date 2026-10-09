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
    """Information about LEXIS Project entity"""

    def __init__(
        self,
        project_name: str,
        project_validity_start: str = "-",
        project_validity_end: str = "-",
    ):
        self._project_name = project_name
        self._project_validity_start = project_validity_start
        self._project_validity_end = project_validity_end

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def project_validity_start(self) -> str:
        return self._project_validity_start

    @property
    def project_validity_end(self) -> str:
        return self._project_validity_end


@dataclass
class QMachineInfo:
    """Parsed response of /heappe/QScheduler/MachineInfo."""

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

    `name` is the LEXIS LocationName, the HEAppE ClusterName and the backend name.
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
    """Information about LEXIS Resource entity and the quantum clusters assigned to it."""

    def __init__(
        self,
        resource_name: str,
        project_resource_id: str,
        resource_validity_start: str,
        resource_validity_end: str,
        clusters: dict[str, QCluster] | None = None,
    ):
        self._resource_name = resource_name
        self._project_resource_id = project_resource_id
        self._resource_validity_start = resource_validity_start
        self._resource_validity_end = resource_validity_end
        self._clusters: dict[str, QCluster] = dict(clusters or {})

    @property
    def resource_name(self) -> str:
        return self._resource_name

    @property
    def project_resource_id(self) -> str:
        """The ID of the resource assignment in LEXIS"""
        return self._project_resource_id

    @property
    def resource_validity_start(self) -> str:
        return self._resource_validity_start

    @property
    def resource_validity_end(self) -> str:
        return self._resource_validity_end

    @property
    def clusters(self) -> dict[str, QCluster]:
        return self._clusters

    @property
    def cluster_names(self) -> list[str]:
        return sorted(self._clusters)

    @property
    def allocated_units(self) -> int:
        """Sum of allocations over all clusters of the resource."""
        return sum(c.allocated_units or 0 for c in self._clusters.values())

    def get_cluster(self, name: str) -> QCluster:
        try:
            return self._clusters[name]
        except KeyError:
            raise KeyError(
                f"Cluster '{name}' not found in resource '{self._resource_name}'. "
                f"Available: {self.cluster_names}"
            ) from None


class QBackendMetadata:
    """Read-only view of one quantum backend (= one QCluster of a LexisResource).

    :args:
        lexis_project: LexisProject
        lexis_resource: LexisResource
        cluster: QCluster
        static_architecture: Optional static architecture (from /heappe/QScheduler/MachineArchitecture)
            For IQM backends, this is an instance of iqm.station_control.interface.models.StaticArchitecture.
        dynamic_architecture: Optional dynamic architecture (from /heappe/QScheduler/CalibrationSet/{calibration_set_id}?Endpoint=dynamic-quantum-architecture)
            For IQM backends, this is an instance of iqm.station_control.interface.models.DynamicArchitecture.
    """

    def __init__(
        self,
        lexis_project: LexisProject,
        lexis_resource: LexisResource,
        cluster: QCluster,
        static_architecture: StaticArchitecture | None = None,
        dynamic_architecture: DynamicArchitecture | None = None,
    ):
        self._lexis_project = lexis_project
        self._lexis_resource = lexis_resource
        self._cluster = cluster
        self._static_architecture = static_architecture
        self._dynamic_architecture = dynamic_architecture

    def __repr__(self) -> str:
        return (
            f"QBackendMetadata(backend_name={self.backend_name!r}, "
            f"available={self.available}, sw_stack={self.software_stack!r}, "
            f"qaas_version={self.qaas_version!r})"
        )

    # --- structure ---------------------------------------------------------

    @property
    def cluster(self) -> QCluster:
        return self._cluster

    @property
    def lexis_project(self) -> LexisProject:
        return self._lexis_project

    @property
    def lexis_resource(self) -> LexisResource:
        return self._lexis_resource

    @property
    def machine_info(self) -> QMachineInfo | None:
        return self._cluster.machine_info

    # --- cluster-level -----------------------------------------------------

    @property
    def backend_name(self) -> str:
        """Cluster name == quantum backend name."""
        return self._cluster.name

    @property
    def host_entity(self) -> str:
        return self._cluster.name

    @property
    def heappe_url(self) -> str:
        return self._cluster.heappe_url

    @property
    def cluster_id(self) -> int | None:
        return self._cluster.cluster_id

    @property
    def cluster_node_type_id(self) -> int | None:
        return self._cluster.cluster_node_type_id

    @property
    def allocated_units(self) -> int | None:
        return self._cluster.allocated_units

    @property
    def host_supercomputer(self) -> str:
        return self._cluster.host_supercomputer

    @property
    def available(self) -> bool:
        return self._cluster.available

    @property
    def error(self) -> str | None:
        return self._cluster.error

    # --- machine info ------------------------------------------------------

    @property
    def qaas_version(self) -> str | None:
        """Version of deployed QaaS backend API (QScheduler)."""
        mi = self._cluster.machine_info
        return mi.qscheduler_version if mi else None

    @property
    def qccsw_version(self) -> str | None:
        mi = self._cluster.machine_info
        return mi.qccsw_version if mi else None

    @property
    def software_stack(self) -> str | None:
        mi = self._cluster.machine_info
        return mi.sw_stack if mi else None

    @property
    def supplier(self) -> str:
        mi = self._cluster.machine_info
        return mi.supplier if mi else "-"

    @property
    def quantum_technology(self) -> str:
        mi = self._cluster.machine_info
        return mi.quantum_technology if mi else "-"

    # --- dynamic architecture ------------------------------------------------
    @property
    def dynamic_architecture(self) -> Any | None:
        """Return the dynamic architecture of the backend, if available."""
        return self._dynamic_architecture

    @property
    def static_architecture(self) -> Any | None:
        """Return the static architecture of the backend, if available."""
        return self._static_architecture
