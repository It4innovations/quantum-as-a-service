from iqm.qiskit_iqm.iqm_provider import IQMFacadeBackend
from uuid import UUID
from .backend_iqm import QBackendIQM
from .client import QClient
from .qpulla import QPulla
from .utils import QException
from .backend_metadata import QBackendMetadata
class QProvider:
    """
    QaaS wrapper around IQMProvider with Lexis token authentication
    """
    
    def __init__(self, token: str, lexis_project:str, provider_access_token=None):
        """Initalize QProvider wrapper

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
        # FIXME:
        return NotImplemented

    def get_backend_info(self, lexis_resource:str)->QBackendMetadata:
        
        client = QClient(self._token, self._lexis_project, lexis_resource, self._provider_token)
        return client.get_quantum_backend_info()
    
    def get_backend(
        self, lexis_resource: str | QBackendMetadata, backend_name: str | None = None, calibration_set_id: UUID | None = None, *, use_metrics: bool = False
    ) -> QBackendIQM:
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
        return None

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
