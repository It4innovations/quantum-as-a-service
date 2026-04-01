"""Implements class storing information about specific quantum backend
"""

class LexisProject:
    """Information about LEXIS Resource entity"""
    def __init__(self, project_name:str, project_validity_start:str="-", project_validity_end:str="-"):
        """
        Initializes a LexisProject object.

        :param project_name: The name of the project.
        :param project_validity_start: The start date of the project.
        :param project_validity_end: The end date of the project.
        """
        self._project_name = project_name
        self._project_validity_start = project_validity_start
        self._project_validity_end = project_validity_end
        
    @property
    def project_name(self):
        """
        The name of the project.
        """
        return self._project_name

    @property
    def project_validity_start(self):
        """
        The start date of the project.
        """
        return self._project_validity_start

    @property
    def project_validity_end(self):
        """
        The end date of the project.
        """
        return self._project_validity_end

class LexisResource:
    """Information about LEXIS Resource entity"""
    def __init__(self, resource_name:str, allocated_units:int, resource_validity_start:str, resource_validity_end:str, heappe_url:str):
        """
        Initializes a LexisResource object.

        :param resource_name: The name of the resource.
        :param allocated_units: The number of units allocated.
        :param resource_validity_start: The start date of the resource.
        :param resource_validity_end: The end date of the resource.
        :param heappe_url: HTTP address of HEAppE url managing concrete instance of QC
        """
        self._resource_name = resource_name
        self._allocated_units = allocated_units
        self._resource_validity_start = resource_validity_start
        self._resource_validity_end = resource_validity_end
        self._heappe_url = heappe_url
    
    @property
    def resource_name(self):
        """
        The name of the resource.
        """
        return self._resource_name

    @property
    def allocated_units(self):
        """
        The number of units allocated.
        """
        return self._allocated_units

    @property
    def resource_validity_start(self):
        """
        The start date of the resource.
        """
        return self._resource_validity_start

    @property
    def resource_validity_end(self):
        """
        The end date of the resource.
        """
        return self._resource_validity_end

    @property
    def heappe_url(self):
        """
        HTTP address of HEAppE url managing concrete instance of QC
        """
        return self._heappe_url

class QBackendMetadata:
    """This class represents information about a quantum backend.
    """
    def __init__(self, backend_name:str, swstack:str, available:bool, host_entity:str, lexis_project:LexisProject, lexis_resource:LexisResource, supplier:str="-", quantum_technology:str="-", host_supercomputer:str="-"):
        """
        This class represents information about a quantum backend.

        :param backend_name: The name of the quantum backend
        :param available: A boolean indicating whether the backend is online and available
        :param host_entity: The hostname or identifier of the quantum computer hosting entity
        :param lexis_resource: Resource registered on LEXIS platform assigned to LEXIS project
        :param lexis_project: Info about LEXIS project assigned to LEXIS resource
        :param supplier: The name of the quantum computer vendor
        :param quantum_technology: The name of the quantum computer technology (e.g. superconducting, simulator)
        :param host_supercomputer: The name of HPC connected to the QC (e.g. Karolina)
        :param swstack: Software stack type (e.g. IQM)
        """
        
        self._backend_name = backend_name
        self._swstack = swstack
        self._available = available
        self._host_entity = host_entity
        self._lexis_project = lexis_project
        self._lexis_resource = lexis_resource
        self._supplier = supplier
        self._quantum_technology = quantum_technology
        self._host_supercomputer = host_supercomputer
    @property
    def backend_name(self)->str:
        """Name of backend, which can be used to get
        """
        return self._backend_name
    @property
    def available(self)->bool:
        """Whether is backend online and available
        """
        return self._available
    @property
    def host_entity(self)->str:
        """Quantum computer hosting entity (e.g. IT4Innovations)
        """
        return self._host_entity
    @property
    def lexis_project(self)->LexisProject:
        """Name of resource registered on LEXIS platform
        """
        return self._lexis_project
    @property
    def lexis_resource(self)->LexisResource:
        """Resource registered on LEXIS platform assigned to LEXIS project
        """
        return self._lexis_resource
    @property
    def host_supercomputer(self)->str:
        """Name of hosting supercomputer
        """
        return self._host_supercomputer
    @property
    def supplier(self)->str:
        """Quantum computer vendor (e.g. IQM)
        """
        return self._supplier
    @property
    def software_stack(self)->str:
        """Quantum computer software stack type
        """
        return self._swstack
    @property
    def quantum_technology(self)->str:
        """Technology name (e.g. superconducting)
        """
        return self._quantum_technology
    
