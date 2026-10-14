"""Implements classes storing information about quantum backend metadata.

This module contains classes for handling metadata related to quantum computing
backends, including project and resource information from LEXIS platform.
"""

class LexisProject:
    """Information about LEXIS Resource entity.

    A class representing a project entity in the LEXIS system, containing
    information about project name and validity dates.

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
        """Initialize a LexisProject object.

        Args:
            project_name (str): The name of the project.
            project_validity_start (str, optional): The start date of the project.
                Defaults to "-".
            project_validity_end (str, optional): The end date of the project.
                Defaults to "-".
        """
        self._project_name = project_name
        self._project_validity_start = project_validity_start
        self._project_validity_end = project_validity_end

    @property
    def project_name(self) -> str:
        """Return the name of the project."""
        return self._project_name

    @property
    def project_validity_start(self) -> str:
        """Return the start date of the project validity."""
        return self._project_validity_start

    @property
    def project_validity_end(self) -> str:
        """Return the end date of the project validity."""
        return self._project_validity_end


class LexisResource:
    """Information about LEXIS Resource entity.

    A class representing a resource entity in the LEXIS system, containing
    detailed information about the resource assignment and its validity period.

    Attributes:
        resource_name (str): The name of the resource.
        allocated_units (int): The number of units allocated to the resource.
        project_resource_id (str): The ID of the resource assignment in LEXIS.
        resource_validity_start (str): The start date of the resource validity.
        resource_validity_end (str): The end date of the resource validity.
        heappe_url (str): HTTP address of HEAppE url managing the QC instance.
    """

    def __init__(
        self,
        resource_name: str,
        allocated_units: int,
        project_resource_id: str,
        resource_validity_start: str,
        resource_validity_end: str,
        heappe_url: str,
    ):
        """Initialize a LexisResource object.

        Args:
            resource_name (str): The name of the resource.
            allocated_units (int): The number of units allocated.
            project_resource_id (str): The ID of the resource assignment in LEXIS.
            resource_validity_start (str): The start date of the resource.
            resource_validity_end (str): The end date of the resource.
            heappe_url (str): HTTP address of HEAppE url managing the QC instance.
        """
        self._resource_name = resource_name
        self._allocated_units = allocated_units
        self._project_resource_id = project_resource_id
        self._resource_validity_start = resource_validity_start
        self._resource_validity_end = resource_validity_end
        self._heappe_url = heappe_url

    @property
    def resource_name(self) -> str:
        """Return the name of the resource."""
        return self._resource_name

    @property
    def allocated_units(self) -> int:
        """Return the number of units allocated."""
        return self._allocated_units

    @property
    def project_resource_id(self) -> str:
        """Return the ID of the resource assignment in LEXIS."""
        return self._project_resource_id

    @property
    def resource_validity_start(self) -> str:
        """Return the start date of the resource validity."""
        return self._resource_validity_start

    @property
    def resource_validity_end(self) -> str:
        """Return the end date of the resource validity."""
        return self._resource_validity_end

    @property
    def heappe_url(self) -> str:
        """Return the HTTP address of HEAppE url managing the QC instance."""
        return self._heappe_url


class QBackendMetadata:
    """This class represents information about a quantum backend.

    A class containing metadata about quantum computing backends, including
    details about the backend's configuration, availability, and connection
    information with LEXIS resources.

    Attributes:
        backend_name (str): The name of the quantum backend.
        qaas_version (str): The deployed version of qaas package on backend.
        available (bool): Whether the backend is online and available.
        host_entity (str): The hostname or identifier of the quantum computer
            hosting entity.
        lexis_project (LexisProject): Information about the LEXIS project
            assigned to the resource.
        lexis_resource (LexisResource): Information about the LEXIS resource
            assigned to the project.
        supplier (str): The name of the quantum computer vendor.
        quantum_technology (str): The name of the quantum computer technology
            (e.g. superconducting, simulator).
        host_supercomputer (str): The name of HPC connected to the QC (e.g. Karolina).
        swstack (str): Software stack type (e.g. IQM).
    """

    def __init__(
        self,
        backend_name: str,
        swstack: str,
        qaas_version: str,
        available: bool,
        host_entity: str,
        lexis_project: LexisProject,
        lexis_resource: LexisResource,
        supplier: str = "-",
        quantum_technology: str = "-",
        host_supercomputer: str = "-",
    ):
        """Initialize a QBackendMetadata object.

        Args:
            backend_name (str): The name of the quantum backend.
            swstack (str): Software stack type (e.g. IQM).
            qaas_version (str): The deployed version of qaas package on backend.
            available (bool): A boolean indicating whether the backend is online
                and available.
            host_entity (str): The hostname or identifier of the quantum computer
                hosting entity.
            lexis_project (LexisProject): Info about LEXIS project assigned to
                LEXIS resource.
            lexis_resource (LexisResource): Resource registered on LEXIS platform
                assigned to LEXIS project.
            supplier (str, optional): The name of the quantum computer vendor.
                Defaults to "-".
            quantum_technology (str, optional): The name of the quantum computer
                technology (e.g. superconducting, simulator). Defaults to "-".
            host_supercomputer (str, optional): The name of HPC connected to the
                QC (e.g. Karolina). Defaults to "-".
        """
        self._backend_name = backend_name
        self._qaas_version = qaas_version
        self._swstack = swstack
        self._available = available
        self._host_entity = host_entity
        self._lexis_project = lexis_project
        self._lexis_resource = lexis_resource
        self._supplier = supplier
        self._quantum_technology = quantum_technology
        self._host_supercomputer = host_supercomputer

    @property
    def backend_name(self) -> str:
        """Return the name of the backend."""
        return self._backend_name

    @property
    def qaas_version(self) -> str:
        """Return the version of the deployed QaaS backend API."""
        return self._qaas_version

    @property
    def available(self) -> bool:
        """Return whether the backend is online and available."""
        return self._available

    @property
    def host_entity(self) -> str:
        """Return the quantum computer hosting entity (e.g. IT4Innovations)."""
        return self._host_entity

    @property
    def lexis_project(self) -> LexisProject:
        """Return information about the LEXIS project."""
        return self._lexis_project

    @property
    def lexis_resource(self) -> LexisResource:
        """Return information about the LEXIS resource."""
        return self._lexis_resource

    @property
    def host_supercomputer(self) -> str:
        """Return the name of the hosting supercomputer."""
        return self._host_supercomputer

    @property
    def supplier(self) -> str:
        """Return the quantum computer vendor (e.g. IQM)."""
        return self._supplier

    @property
    def software_stack(self) -> str:
        """Return the quantum computer software stack type."""
        return self._swstack

    @property
    def quantum_technology(self) -> str:
        """Return the technology name (e.g. superconducting)."""
        return self._quantum_technology
