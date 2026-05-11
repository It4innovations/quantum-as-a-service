from iqm.qiskit_iqm.iqm_provider import IQMFacadeBackend
from uuid import UUID
from .backend_iqm import QBackendIQM
from .client import QClient
from .qpulla import QPulla
from .backend_metadata import QBackendMetadata
class QProvider:
    """
    QaaS wrapper around IQMProvider with Lexis token authentication
    """
    
    def __init__(self, token: str, lexis_project:str, provider_access_token=None):
        """Initialize QProvider wrapper

        :param token: LEXIS access token
        :param lexis_project: LEXIS project aggregating users and resources for computation and storing
        :param provider: Requested provider type. Currently available types is "IQM", defaults to "IQM"
        :param additional_access_token: Access token If Quntum provider request another layer of auth
        """
        # NOTE: If possible try to unite authorization tokens and trust
        
        self._lexis_project = lexis_project
        self._token = token
        self._provider_token = provider_access_token
    
    @classmethod
    def list_available_backends(cls, token:str, lexis_project:str, provider_access_token=None)->list[QBackendMetadata]:
        """List available quantum backends for given LEXIS project and provider access token
        
        :param token: LEXIS access token
        :param lexis_project: LEXIS project short name, e.g. "vlq_demo_project"
        :param provider_access_token: Access token If Quantum provider request another layer of auth
        :return: List of QBackendMetadata instances with available backends information
        """

        client = QClient(token, lexis_project, provider_token=provider_access_token)
        
        return client.get_available_backends()

    def get_backend_info(self, lexis_resource:str, quantum_computer_name:str)->QBackendMetadata:
        """Get backend information about quantum computer based on resource and assignment name (quantum computer name)
        :param lexis_resource: LEXIS resource name, e.g. "VLQ-CZ"
        :param quantum_computer_name: Quantum computer name, e.g. "VLQ", its equal to AggregationName in LEXIS Resources.Assignments
        
        :return: QBackendMetadata instance with backend information
        """
        client = QClient(self._token, self._lexis_project, lexis_resource, quantum_computer_name, self._provider_token)
        return client.get_quantum_backend_info()
    
    def get_backend(
        self, lexis_resource: str | QBackendMetadata, backend_name: str | None = None, calibration_set_id: UUID | None = None, *, use_metrics: bool = False
    ) -> QBackendIQM :
        """An IQMBackend instance associated with this provider.

        
        :param backend_name: optional name of a custom facade backend
        :param lexis_resource_name: LEXIS accounting resource, defaults to 'VLQ HPC'
        :param calibration_set_id: ID of the calibration set used to create the transpilation target of the backend.
            If None, the server default calibration set will be used.

        """
        
        if isinstance(lexis_resource, QBackendMetadata):
            lexis_resource_name:str = lexis_resource.lexis_resource_name
        else:
            lexis_resource_name:str = lexis_resource
        
        client = QClient(self._token, self._lexis_project, lexis_resource_name, self._provider_token)
        backend_metadata = client.get_quantum_backend_info()
        

        if backend_metadata.software_stack == "IQM":
            if backend_name and backend_name.startswith("facade_"):
                return IQMFacadeBackend(client,
                                        name=backend_name,
                                        calibration_set_id=calibration_set_id,
                                        use_metrics=use_metrics)
            return QBackendIQM(client,
                               calibration_set_id=calibration_set_id,
                               use_metrics=use_metrics, backend_metadata=backend_metadata)
        return NotImplemented

    def get_pulla(self, lexis_resource: str | QBackendMetadata) -> QPulla:
        if isinstance(lexis_resource, QBackendMetadata):
            lexis_resource:str = lexis_resource.lexis_resource_name
        else:
            lexis_resource:str = lexis_resource
            
        client = QClient(self._token, self._lexis_project, lexis_resource, self._provider_token)
        pulla_data, pulla = client.get_pulla()
        return QPulla(client, pulla, **pulla_data)

    def get_client(self, lexis_resource: str | QBackendMetadata) -> QClient:
        if isinstance(lexis_resource, QBackendMetadata):
            lexis_resource:str = lexis_resource.lexis_resource_name
        else:
            lexis_resource:str = lexis_resource
        
        c = QClient(self._token, self._lexis_project, lexis_resource, self._provider_token)
        return c
