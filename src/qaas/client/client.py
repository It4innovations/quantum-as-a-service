from typing import Optional, Dict, Any, Tuple
import time
from uuid import UUID
import json
import base64
import logging
import os
import sys
from tempfile import NamedTemporaryFile
from datetime import datetime
import concurrent
import pickle
import ssl
import truststore
import dill
import requests
import jwt
from jwt import PyJWKClient
from iqm.station_control.interface.models import SweepDefinition
from iqm.iqm_server_client.models import CalibrationSet
from iqm.pulla.pulla import Pulla


from qiskit import QuantumCircuit
from iqm.qiskit_iqm import IQMBackend
from iqm.station_control.interface.models import DynamicQuantumArchitecture

from py4heappe.heappe_v6.core import ApiClient as HEAppEApi, Configuration as HEAppEConfiguration
from py4heappe.heappe_v6.core.models import (
    LexisCredentialsExt as LexisCredentials,
    AuthenticateLexisTokenModel,
    CreateJobByProjectModel,
    SubmitJobModel,
    CancelJobModel,
    JobSpecificationExt as JobSpecification,
    TaskSpecificationExt as TaskSpecification,
    DownloadFileFromClusterModel,
    ClusterExt,
    ClusterNodeTypeExt,
    ProjectExt,
    CommandTemplateExt,
    SubmittedJobInfoExt
)
from py4heappe.heappe_v6.core.models import (
    EnvironmentVariableExt
)
from py4heappe.heappe_v6.core.api import (
    UserAndLimitationManagementApi,
    JobManagementApi,
    FileTransferApi,
    ClusterInformationApi
)
from py4heappe.heappe_v6.core.rest import ApiException

from .utils import QException, QAuthException, QResultsFailed, JobState
from .backend_metadata import QBackendMetadata, LexisResource, LexisProject

from .cryption_control import encrypt_string, generate_password

# -----------
# Set Logging
# -----------

log = logging.getLoggerClass()(__name__, os.environ.get(
    'QPROVIDER_LOGLEVEL', 'INFO').upper())

# Formatter for consistent output
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# Decide handler: file or stderr
logfile = os.environ.get("QPROVIDER_LOGFILE")
if logfile:
    handler = logging.FileHandler(logfile, mode="a")
else:
    handler = logging.StreamHandler(sys.stderr)

handler.setFormatter(formatter)
log.addHandler(handler)


# -----------------------
# QClient Implementation
# -----------------------


class QClient:
    """
    Client for quantum backend communication with LEXIS token authentication via HEAppE.

    This class provides comprehensive authentication and communication with the LEXIS platform
    and HEAppE infrastructure for quantum computing services. It handles JWT token validation,
    project authorization, resource allocation, and HEAppE session management for quantum
    job submission and execution.

    :param token: LEXIS JWT authentication token
    :type token: str
    :param lexis_project: LEXIS project identifier for resource access
    :type lexis_project: str
    :param lexis_resource_name: Optional specific resource name within project
    :type lexis_resource_name: str or None
    :param quantum_computer_name: 
    :param backend_url: Optional custom backend URL override
    :type backend_url: str or None

    :raises QException: When client initialization or configuration fails
    :raises QAuthException: When authentication or authorization fails
    :raises QResultsFailed: When failed to execute HEAppE job or retrieve results of a job

    :cvar DEFAULT_LEXIS_ASSIGNMENT_LOCATION_NAME: Default quantum location name
    :vartype DEFAULT_LEXIS_ASSIGNMENT_LOCATION_NAME: str
    :cvar DEFAULT_TEMPLATE_NAME: Default HEAppE command template name  
    :vartype DEFAULT_TEMPLATE_NAME: str
    :cvar USERORG_BASE_URL: Base URL for LEXIS UserOrg API
    :vartype USERORG_BASE_URL: str

    :cvar DEFAULT_POLL_TIME: Default polling interval in seconds for job status checks
    :vartype DEFAULT_POLL_TIME: int

    .. note::
        Authentication is performed automatically during initialization, including
        JWT token validation, project membership verification, resource authorization,
        and HEAppE session establishment.

    Example:
        >>> client = QClient(token, "my_project", "qaas_user")
        >>> backend_info = client.get_quantum_backend_info("iqm_backend")
        >>> job_id = client.submit_quantum_job(job_specification)
    """

    DEFAULT_LEXIS_AGGREGATION_NAME = ["VLQ", "EQE1", "QLM"]
    # Two templates for two different queues are required by HEAppE architecture
    DEFAULT_QINIT_TEMPLATE_NAME = "RUN_QINIT"
    DEFAULT_QEXECUTE_TEMPLATE_NAME = "RUN_QEXECUTE"
    DEFAULT_QINIT_QUEUE_NAME = "init_queue"
    DEFAULT_QEXECUTE_QUEUE_NAME = "compute_queue"
    DEFAULT_USERORG_BASE_URL = "https://api.lexis.tech/userorg"

    DEFAULT_QUANTUM_LOCATION_TYPE = 7

    # HEAppE 6.2 endpoints (not yet in client)
    UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT = "/heappe/FileTransfer/UploadFilesToJobExecutionDir"
    
    DEFAULT_POLL_TIME = 0.5

    def __init__(self, token: str, lexis_project: str, lexis_resource_name: str | None = None, quantum_computer_name: str | None = None, provider_token:str=None, **kwargs):
        """
        Initialize QClient with LEXIS authentication and project configuration.

        Performs complete authentication workflow including JWT token validation,
        LEXIS project membership verification, resource authorization, and HEAppE
        session establishment. All authentication steps must succeed for
        initialization to complete.

        :param token: Valid LEXIS JWT authentication token
        :type token: str
        :param lexis_project: LEXIS project identifier for quantum resource access
        :type lexis_project: str
        :param lexis_resource_name: Specific resource name within project (auto-detected if None)
        :type lexis_resource_name: str or None
        :param quantum_computer_name: Name of the quantum computer within the resource (auto-detected if None), equal to AggregationName in LEXIS Resources.Assignments
        :type quantum_computer_name: str or None

        :param provider_token: When provider requires additional authentication like API token
        :type provider_token: str or None

        :raises QException: When initialization fails due to configuration errors
        :raises QAuthException: When any authentication step fails (token, project, resource, HEAppE)
        :raises QResultsFailed: When HEAppE authentication job fails
        :raises ValueError: When JWT token format is invalid
        :raises requests.RequestException: When LEXIS API calls fail

        .. warning::
            Initialization may take several seconds as it involves multiple API calls
            to LEXIS UserOrg service and HEAppE authentication workflow.
        """
        self._token = token
        self._lexis_project = lexis_project
        self.provider_token = provider_token
        self._heappe_client: Optional[HEAppEApi] = None
        # Caution, this attribute is changed by all authentication functions during initialization
        self._authenticated = False  # FIXME: check that all login flows are correctly handled

        # These will be populated during resource authorization
        self._project_id = None
        self._cluster_id = None

        
        # Operational kwargs
        self._lexis_userorg_api_url = kwargs.get("lexis_userorg_api_url", self.DEFAULT_USERORG_BASE_URL)
        
        
        # Authenticate on initialization
        self._username, self._lexis_project_info = self._authenticate_authorize_lexis()
        self._authenticated = True

        self._backend_metadata = self._authorize_lexis_resource(lexis_resource_name, quantum_computer_name)
        self._heappe_client = self._authenticate_heappe()
        self._command_template_infos = self._get_command_template_ids(
            template_name_qinit=kwargs.get("qinit_command_template_name", self.DEFAULT_QINIT_TEMPLATE_NAME),
            template_name_qexecute=kwargs.get("qexecute_command_template_name", self.DEFAULT_QEXECUTE_TEMPLATE_NAME),
            qinit_queue_name=kwargs.get("qinit_queue_name", self.DEFAULT_QINIT_QUEUE_NAME),
            qexecute_queue_name=kwargs.get("qexecute_queue_name", self.DEFAULT_QEXECUTE_QUEUE_NAME)
            )

        # Architecture
        self._dynamic_quantum_architectures = {}
        

    def _authenticate_authorize_lexis(self) -> Tuple[str, Dict[str, Any]]:
        """
        Validate JWT token and verify LEXIS project access.

        Performs comprehensive JWT token validation including signature verification
        using JWKS, expiration checking, and project membership verification through
        the LEXIS UserOrg API. Also validates project active/inactive status based
        on start and end dates.

        :returns: Tuple of (username, project_info_dict)
        :rtype: Tuple[str, Dict[str, Any]]

        :raises QException: When token validation or project verification fails
        :raises QAuthException: When authentication fails at any stage
        :raises QResultsFailed: When LEXIS API calls fail
        :raises ValueError: When JWT token format or claims are invalid
        :raises requests.RequestException: When UserOrg API requests fail

        .. note::
            Uses PyJWKClient to automatically fetch and verify JWT signing keys
            from the Keycloak JWKS endpoint derived from the token issuer.

        The method validates:

        * JWT signature using JWKS from token issuer
        * Token expiration timestamp
        * Project membership via UserOrg API
        * Project active status (start/end dates)
        """

        try:
            ############################################################
            # Extract issuer from JWT token without verification first #
            ############################################################
            # This allows us to get the Keycloak base URL dynamically
            unverified_token = jwt.decode(
                self._token, options={"verify_signature": False})
            keycloak_base_url = unverified_token.get('iss')

            if not keycloak_base_url:
                raise ValueError("JWT token missing issuer (iss) claim")

            log.debug("Detected JWT issuer: %s", keycloak_base_url)

            # Construct JWKS URL from issuer
            jwks_url = f"{keycloak_base_url}/protocol/openid-connect/certs"

            log.debug("JWKS: %s", str(jwks_url))

            # Initialize JWKS client
            if sys.platform == "win32":
                ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            else:
                ctx = ssl.create_default_context()
            jwks_client = PyJWKClient(jwks_url,ssl_context=ctx)

            # Get signing key from JWT header
            signing_key = jwks_client.get_signing_key_from_jwt(self._token)

            # Now decode and fully verify JWT token
            decoded_token = jwt.decode(
                self._token,
                signing_key.key,
                algorithms=["RS256"],
                # audience="lexis-portal",  # Can be extracted from token if needed
                issuer=keycloak_base_url,
                audience="portal",
                # Removed verify_aud for flexibility
                options={"verify_exp": True, "verify_iss": True}
            )

            # Extract user information from token
            user_id = decoded_token.get(
                'preferred_username') or decoded_token.get('sub')

            if not user_id:
                raise QAuthException(
                    reason="Invalid token: missing user information",
                    user_id=user_id,
                    resource=self._lexis_project
                )

            # Check token expiration
            exp_timestamp = decoded_token.get('exp')
            if exp_timestamp and exp_timestamp < time.time():
                raise QAuthException(
                    reason="Token has expired",
                    user_id=user_id,
                    resource=self._lexis_project
                )

            log.debug("JWT token validated successfully for user: %s",
                      user_id)

        except Exception as e:
            log.error(e)
            raise QAuthException(
                resource=self._lexis_project
            ) from e

        try:
            ##############################################
            # Check project membership using UserOrg API #
            ##############################################

            headers = {
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json'
            }

            # Get user's project memberships
            user_projects_url = f"{self._lexis_userorg_api_url}/api/Project"

            response = requests.get(
                user_projects_url, headers=headers, params={'ProjectShortName': self._lexis_project}, timeout=30)
            response.raise_for_status()

            projects_data = response.json()

            # Check if user is member of the specified LEXIS project
            project_found = False
            project_info = None

            for project in projects_data:
                # Check both 'id' and 'name' fields for project identification
                project_shortname = project.get('ShortName')

                if project_shortname == self._lexis_project:
                    project_found = True
                    project_info = project
                    break

            if not project_found:
                available_projects = set()
                for proj in projects_data:
                    proj_identifier = proj.get('ShortName')
                    if proj_identifier:
                        available_projects.add(proj_identifier)

                raise QAuthException(
                    reason=f"User does not have access to LEXIS project '{self._lexis_project}'. Available projects: {available_projects}",
                    user_id=user_id,
                    resource=self._lexis_project
                )

            log.debug("Project access verified: %s", self._lexis_project)
            if project_info:
                log.debug("Project details: %s ", str(project_info))

        except Exception as e:
            raise QAuthException(
                user_id=user_id,
                resource=self._lexis_project
            ) from e

        ###############################################
        # Verify project is active/enabled #
        ###############################################
        project_start_date = project_info.get(
            'StartDate', None)
        project_end_date = project_info.get(
            'EndDate', None)

        if project_start_date and project_end_date:
            try:

                # Parse ISO format dates
                start_dt = datetime.fromisoformat(
                    project_start_date.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(
                    project_end_date.replace('Z', '+00:00'))
                current_dt = datetime.now(
                    start_dt.tzinfo) if start_dt.tzinfo else datetime.now()

                if current_dt < start_dt:
                    raise QAuthException(
                        reason=f"Project '{self._lexis_project}' has not started yet. Start date: {project_start_date}",
                        user_id=user_id,
                        resource=self._lexis_project
                    )
                elif current_dt > end_dt:
                    raise QAuthException(
                        reason=f"Project '{self._lexis_project}' has expired. End date: {project_end_date}",
                        user_id=user_id,
                        resource=self._lexis_project
                    )
                else:
                    log.debug("Project is active from %s to %s",
                              project_start_date, project_end_date)

            except ValueError as date_error:
                log.warning("Could not parse project dates: %s", date_error)
                raise QAuthException(
                    "Could not parse project dates") from date_error

        return user_id, project_info

    def _authorize_lexis_resource(self, lexis_resource_name: str | None = None, quantum_computer_name: str | None = None) -> QBackendMetadata:
        """
        Authorize access to quantum resources within LEXIS project.

        Retrieves project resources from LEXIS UserOrg API and validates access
        to quantum computing resources. Automatically detects VLQ (quantum) resources
        or validates access to specifically named resources. Extracts HEAppE URL
        configuration from resource specifications.

        :param lexis_resource_name: Specific resource name to authorize (auto-detect if None)
        :type lexis_resource_name: str or None

        :param quantum_computer_name: Name of the quantum computer within the resource (auto-detected if None), equal to AggregationName in LEXIS Resources.Assignments
        :type quantum_computer_name: str or None

        :returns: QBackend information (LEXIS project, LEXIS resource, QBackend metadata)
        :rtype: QBackendMetadata

        :raises QException: When resource configuration is invalid
        :raises QAuthException: When resource access is denied or not found
        :raises QResultsFailed: When LEXIS API calls fail
        :raises requests.RequestException: When UserOrg API requests fail

        .. note::
            If lexis_resource_name is None, automatically searches for resources
            with DEFAULT_LEXIS_ASSIGNMENT_LOCATION_NAME ("VLQ") assignment type.

        The method validates:

        * Resource existence within project
        * VLQ assignment availability  
        * Resource active status (start/end dates)
        * HEAppE URL presence in specifications
        """

        if not self._authenticated:
            raise QAuthException("Unauthorized!!!")

        try:
            headers = {
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json'
            }

            # Get project resources
            project_resources_url = f"{self._lexis_userorg_api_url}/api/ProjectResource"
            response = requests.get(project_resources_url, headers=headers, params={
                                    'ProjectShortName': self._lexis_project}, timeout=30)
            response.raise_for_status()

            project_resources = response.json()

            log.debug("Project resource: %s", str(project_resources))

            # Find the project by name or ID
            project_resource_info = None
            assignment_info = None
            if lexis_resource_name is None:  # Try to find automatically
                for resource in project_resources:
                    for assignment in resource.get('Assignments', []):
                        location_type_id = resource.get('LocationTypeId')
                        if location_type_id == QClient.DEFAULT_QUANTUM_LOCATION_TYPE:  # LocationTypeId 7 corresponds to locations in LEXIS, which we use for quantum backends
                            aggregation_name = resource.get('AggregationName')
                            if aggregation_name in QClient.DEFAULT_LEXIS_AGGREGATION_NAME:
                                if quantum_computer_name is None:
                                    assignment_info = assignment
                                    project_resource_info = resource
                                    break
                                elif quantum_computer_name == aggregation_name:
                                    assignment_info = assignment
                                    project_resource_info = resource
                                    break
                    if assignment_info and project_resource_info:
                        break

                if not project_resource_info:
                    raise QAuthException(
                        reason=f"No resource with VLQ found for LEXIS project '{self._lexis_project}', please verify you have assigned one!",
                        user_id=self._username
                    )
            else:
                for resource in project_resources:
                    resource_name = resource.get('Name')
                    if resource_name == lexis_resource_name:
                        project_resource_info = resource
                        for assignment in resource.get('Assignments', []):
                            location_name = assignment.get('LocationName')
                            if location_name in QClient.DEFAULT_LEXIS_AGGREGATION_NAME:
                                assignment_info = assignment
                                break
                        break

            if not project_resource_info or not assignment_info:
                raise QAuthException(
                    reason=f"Resource or assignment of type {QClient.DEFAULT_LEXIS_AGGREGATION_NAME} not found in available resources for project '{self._lexis_project}'",
                    user_id=self._username,
                    resource=lexis_resource_name
                )

            ###############################################
            # Verify resource is active/enabled #
            ###############################################
            resource_start_date = project_resource_info.get(
                'StartDate', None)
            resource_end_date = project_resource_info.get(
                'EndDate', None)

            if resource_start_date and resource_end_date:
                try:

                    # Parse ISO format dates
                    start_dt = datetime.fromisoformat(
                        resource_start_date.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(
                        resource_end_date.replace('Z', '+00:00'))
                    current_dt = datetime.now(
                        start_dt.tzinfo) if start_dt.tzinfo else datetime.now()

                    if current_dt < start_dt:
                        raise QAuthException(
                            reason=f"Project '{self._lexis_project}' has not started yet. Start date: {resource_start_date}",
                            user_id=self._username,
                            resource=self._lexis_project
                        )
                    elif current_dt > end_dt:
                        raise QAuthException(
                            reason=f"Project '{self._lexis_project}' has expired. End date: {resource_end_date}",
                            user_id=self._username,
                            resource=self._lexis_project
                        )
                    else:
                        log.debug("Project is active from %s to %s",
                                  resource_start_date, resource_end_date)

                except ValueError as date_error:
                    log.warning(
                        "Could not parse project dates: %s", date_error)

                # Extract HEAppE URL, SW_STACK, QUANTUM_TECHNOLOGY from specifications
                heappe_url = None
                sw_stack = None
                quantum_technology = None
                for spec in assignment_info.get('Specifications', []):
                    if spec.get('Key') == 'HEAPPE_URL':
                        heappe_url = spec.get('Value')
                    elif spec.get('Key') == 'SW_STACK':
                        sw_stack = spec.get('Value')
                    elif spec.get('Key') == 'QUANTUM_TECHNOLOGY':
                        quantum_technology = spec.get('Value')
                    # stop searching when all keys was found
                    if heappe_url and sw_stack and quantum_technology:
                        break
                        
                # Check whether all required keys was found
                if not heappe_url:
                    raise QAuthException(
                        reason="HEAPPE_URL not found in resource specifications",
                        user_id=self._username,
                        resource=self._lexis_project
                    )
                if not sw_stack:
                    raise QAuthException(
                        reason="SW_STACK not found in resource specifications",
                        user_id=self._username,
                        resource=self._lexis_project
                    )
                
                # quantum technology is optional

                log.debug("Found SW_STACK: %s, QUANTUM_TECHNOLOGY: %s, HEAppE URL: %s for resource: %s",
                          sw_stack, "-" if not quantum_technology else quantum_technology, heappe_url, lexis_resource_name)

                backend_info = QBackendMetadata(
                    backend_name=assignment_info["AggregationName"],
                    swstack=sw_stack,
                    available="UNKNOWN", #FIXME: get this information
                    quantum_technology=quantum_technology,
                    lexis_resource=LexisResource(
                        project_resource_info["Name"],
                        assignment_info["AllocationAmount"],
                        assignment_info["ProjectResourceId"],
                        project_resource_info["StartDate"],
                        project_resource_info["EndDate"],
                        heappe_url
                        ),
                    lexis_project=LexisProject(self._lexis_project),
                    host_entity=assignment_info["LocationName"]
                )

                return backend_info

        except QAuthException:
            raise
        except Exception as e:
            raise QAuthException(
                reason=f"Failed to authorize LEXIS resource: {str(e)}",
                user_id=self._username,
                resource=self._lexis_project
            ) from e

    def _authenticate_heappe(self)->HEAppEApi:
        """
        Prepares HEAppE client for communication with HEAppE instances connected to QCs.

        :raises QException: When HEAppE client configuration fails
        :raises QAuthException: When HEAppE authentication fails or returns invalid session
        :raises QResultsFailed: When HEAppE authentication request fails
        """

        try:
            # Setup file with trusted public certificate
            ca_file = NamedTemporaryFile('w', encoding='utf-8', delete=False)

            ca_file.write(
                """-----BEGIN CERTIFICATE-----
MIIFOjCCBMCgAwIBAgIQOKl0rK8uz3jWbelvhH3T5TAKBggqhkjOPQQDAzBgMQsw
CQYDVQQGEwJHUjE3MDUGA1UECgwuSGVsbGVuaWMgQWNhZGVtaWMgYW5kIFJlc2Vh
cmNoIEluc3RpdHV0aW9ucyBDQTEYMBYGA1UEAwwPR0VBTlQgVExTIEVDQyAxMB4X
DTI1MDkxMTA3MDcxOVoXDTI2MDkxMTA3MDcxOVowgaMxCzAJBgNVBAYTAkNaMR4w
HAYDVQQIDBVNb3JhdnNrb3NsZXpza8O9IGtyYWoxEDAOBgNVBAcMB09zdHJhdmEx
QTA/BgNVBAoMOFZ5c29rw6EgxaFrb2xhIGLDocWIc2vDoSAtIFRlY2huaWNrw6Eg
dW5pdmVyeml0YSBPc3RyYXZhMR8wHQYDVQQDDBZxdWFudHVtLmhlYXBwZS5pdDRp
LmN6MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAEmQqFWnIuktS0Csyx1a12kl146wdI
VRRmEf6F0BJs9VpLaipuma0LJkPMVqN6UGS211HPQsaVA7qXla9lEgcDFs+QPnbg
QT/aMVmaZ3neUOVWR7Ysn2JAGNMlTRfgZ0a1o4IC+TCCAvUwHwYDVR0jBBgwFoAU
6ZkGjRcfq/uWGlrIW15dXuzanI8wbwYIKwYBBQUHAQEEYzBhMDgGCCsGAQUFBzAC
hixodHRwOi8vY3J0LmhhcmljYS5nci9IQVJJQ0EtR0VBTlQtVExTLUUxLmNlcjAl
BggrBgEFBQcwAYYZaHR0cDovL29jc3AtdGxzLmhhcmljYS5ncjAhBgNVHREEGjAY
ghZxdWFudHVtLmhlYXBwZS5pdDRpLmN6MC0GA1UdIAQmMCQwCAYGZ4EMAQICMAgG
BgQAj3oBBzAOBgwrBgEEAYHPEQEBAQIwHQYDVR0lBBYwFAYIKwYBBQUHAwIGCCsG
AQUFBwMBMD0GA1UdHwQ2MDQwMqAwoC6GLGh0dHA6Ly9jcmwuaGFyaWNhLmdyL0hB
UklDQS1HRUFOVC1UTFMtRTEuY3JsMB0GA1UdDgQWBBRxuKtRV278ArCmmHc4Rp8h
N/RrWzAOBgNVHQ8BAf8EBAMCB4AwggGABgorBgEEAdZ5AgQCBIIBcASCAWwBagB3
AJSxwYqw0FfEe+CsBA4fLLyNw3Vye8lR8gpSYSaGO6c8AAABmTei0u0AAAQDAEgw
RgIhAL4OsS2+pJYNhEsCZq646E14jsPXm2/45vG/+wU+BbzxAiEAsfyJShFPOYSN
IE1/34C5V9qfiIxZhZORjvQzk2o1gD0AdwCUTkOH+uzB74HzGSQmqBhlAcfTXzgC
AT9yZ31VNy4Z2AAAAZk3otLWAAAEAwBIMEYCIQDrqDTALUdHksprY3yqNJDrUmtN
P1VON5OyK6+K/MQ7TQIhAMJNrXXnutC71p8TJB8zSq7IPLUpnUuhfmE1BT15OSjs
AHYA2AlVO5RPev/IFhlvlE+Fq7D4/F6HVSYPFdEucrtFSxQAAAGZN6LS6QAABAMA
RzBFAiEAlL/WpPWJ9M752QBf011d4uIRb8JLuDJnWUQgQQnpdT4CIFbChHHfedHx
Gd24iKV5QHUpmRS3TA5J9aLxmT30n6hSMAoGCCqGSM49BAMDA2gAMGUCMQCi+jsR
XYUXbdKYDnghXDUoF/m3Z/9dXhUM+rkkhoVmSgvMHUyNfmODonDxKYXw3+YCMBHo
dhGSF2mJaMmtuucd73U8UbMO3Zr9otOO7NBcg7St8uPya19bBU55a4SRHKet1Q==
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIDNzCCArygAwIBAgIQQv3c4SYWB+Gl5pNaQAFh3TAKBggqhkjOPQQDAzBsMQsw
CQYDVQQGEwJHUjE3MDUGA1UECgwuSGVsbGVuaWMgQWNhZGVtaWMgYW5kIFJlc2Vh
cmNoIEluc3RpdHV0aW9ucyBDQTEkMCIGA1UEAwwbSEFSSUNBIFRMUyBFQ0MgUm9v
dCBDQSAyMDIxMB4XDTI1MDEwMzExMTQyMVoXDTM5MTIzMTExMTQyMFowYDELMAkG
A1UEBhMCR1IxNzA1BgNVBAoMLkhlbGxlbmljIEFjYWRlbWljIGFuZCBSZXNlYXJj
aCBJbnN0aXR1dGlvbnMgQ0ExGDAWBgNVBAMMD0dFQU5UIFRMUyBFQ0MgMTB2MBAG
ByqGSM49AgEGBSuBBAAiA2IABANPWLwh0Za2UqtbLV7/qNRm78zsttgSuvhn73bU
GtxETsVOEZeMUfMjgHw8EwrsSJI9oj0CgZQFFSEY1NJfcxA/NJiOYJUKPsFbpOrY
dr0q4g+aBZsXWeh7bMCzx24g/aOCAS0wggEpMBIGA1UdEwEB/wQIMAYBAf8CAQAw
HwYDVR0jBBgwFoAUyRtTgRL+BNUW0aq8mm+3oJUZbsowTQYIKwYBBQUHAQEEQTA/
MD0GCCsGAQUFBzAChjFodHRwOi8vY3J0LmhhcmljYS5nci9IQVJJQ0EtVExTLVJv
b3QtMjAyMS1FQ0MuY2VyMBEGA1UdIAQKMAgwBgYEVR0gADAdBgNVHSUEFjAUBggr
BgEFBQcDAgYIKwYBBQUHAwEwQgYDVR0fBDswOTA3oDWgM4YxaHR0cDovL2NybC5o
YXJpY2EuZ3IvSEFSSUNBLVRMUy1Sb290LTIwMjEtRUNDLmNybDAdBgNVHQ4EFgQU
6ZkGjRcfq/uWGlrIW15dXuzanI8wDgYDVR0PAQH/BAQDAgGGMAoGCCqGSM49BAMD
A2kAMGYCMQD2M1caaY2OwmthgmANUQg3LBLI0/2LiCdxa2zNq0G59wVzbjEk0cR/
px52OegIwRACMQCk+iTmBlR6Xfv6igiiaFiPYfN2HfbcYLWbot5DZ2H1b4JVJV+V
rga7uu50SDG9hf4=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICVDCCAdugAwIBAgIQZ3SdjXfYO2rbIvT/WeK/zjAKBggqhkjOPQQDAzBsMQsw
CQYDVQQGEwJHUjE3MDUGA1UECgwuSGVsbGVuaWMgQWNhZGVtaWMgYW5kIFJlc2Vh
cmNoIEluc3RpdHV0aW9ucyBDQTEkMCIGA1UEAwwbSEFSSUNBIFRMUyBFQ0MgUm9v
dCBDQSAyMDIxMB4XDTIxMDIxOTExMDExMFoXDTQ1MDIxMzExMDEwOVowbDELMAkG
A1UEBhMCR1IxNzA1BgNVBAoMLkhlbGxlbmljIEFjYWRlbWljIGFuZCBSZXNlYXJj
aCBJbnN0aXR1dGlvbnMgQ0ExJDAiBgNVBAMMG0hBUklDQSBUTFMgRUNDIFJvb3Qg
Q0EgMjAyMTB2MBAGByqGSM49AgEGBSuBBAAiA2IABDgI/rGgltJ6rK9JOtDA4MM7
KKrxcm1lAEeIhPyaJmuqS7psBAqIXhfyVYf8MLA04jRYVxqEU+kw2anylnTDUR9Y
STHMmE5gEYd103KUkE+bECUqqHgtvpBBWJAVcqeht6NCMEAwDwYDVR0TAQH/BAUw
AwEB/zAdBgNVHQ4EFgQUyRtTgRL+BNUW0aq8mm+3oJUZbsowDgYDVR0PAQH/BAQD
AgGGMAoGCCqGSM49BAMDA2cAMGQCMBHervjcToiwqfAircJRQO9gcS3ujwLEXQNw
SaSS6sUUiHCm0w2wqsosQJz76YJumgIwK0eaB8bRwoF8yguWGEEbo/QwCZ61IygN
nxS2PFOiTAZpffpskcYqSUXm7LcT4Tps
-----END CERTIFICATE-----"""
            )
            ca_file.flush()
            ca_file.close()

            # Initialize HEAppE client
            conf: HEAppEConfiguration = HEAppEConfiguration()
            conf.host = self._backend_metadata.lexis_resource.heappe_url
            conf.ssl_ca_cert = ca_file.name
            
            heappe_client: HEAppEApi = HEAppEApi(
                conf,
                header_name='Authorization',
                header_value=f"Bearer {self._token}"
            )
            return heappe_client

        except Exception as e:
            raise QAuthException(
                f"HEAppE authentication failed: {str(e)}", self._username, f"{self._lexis_project}") from e

    def _python_object_upload_to_cluster(self, python_object, target_file_name:str, job_info:SubmittedJobInfoExt, use_dill=False):
        """Uploads python 
        
        :param python_object: Python object to pickle and send to HEAppE job (job should be already created)
        :param target_file_name: Target file name without extension. (.pkl will be appended)
        :param use_dill: Dill can serialize lambda funcs,defaults to False
        :raises QException: on upload failure
        """
        files = {'files': (target_file_name+".pkl",
                                pickle.dumps(python_object,protocol=pickle.HIGHEST_PROTOCOL) if not use_dill else dill.dumps(python_object, protocol=dill.HIGHEST_PROTOCOL),
                                'application/octet-stream')}

        full_upload_file_url = self._backend_metadata.lexis_resource.heappe_url + \
                QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={'Authorization': f"Bearer {self._token}"},
            timeout=120,
            params={'JobId': job_info.id, 'TaskId': job_info.tasks[0].id})

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        #pylint: disable=W0105
        """
        [
            {
                "FileName": "string",
                "Succeeded": true,
                "Path": "string"
            }
        ]
        """
        uploaded_files = upload_resp.json()
        # check if succeeded
        for _, f in enumerate(uploaded_files):
            if f['Succeeded'] and target_file_name+'.pkl' == f['FileName']:
                return
             
             
        raise QException(
        f"Job meta files failed to be uploaded. Assigned file name is '{target_file_name}'"
        )
    
    def _circuit_upload_to_cluster(self, circuit:str, target_file_name:str, job_info:SubmittedJobInfoExt):
        """Uploads python 
        
        :param circuit: OpenQASM circuit
        :param target_file_name: Target file name without extension. (.qasm will be appended)
        :param job_info: Submitted job info
        :type SubmittedJobInfoExt:
        :raises QException: on upload failure

        """
        
        
        
        file_name = target_file_name+".qasm"
        files = {'files': (file_name,
                                circuit,
                                'text/plain')}

        full_upload_file_url = self._backend_metadata.lexis_resource.heappe_url + \
                QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={'Authorization': f"Bearer {self._token}"},
            timeout=120,
            params={'JobId': job_info.id, 'TaskId': job_info.tasks[0].id})

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        #pylint: disable=W0105
        """
        [
            {
                "FileName": "string",
                "Succeeded": true,
                "Path": "string"
            }
        ]
        """
        uploaded_files = upload_resp.json()
        # check if succeeded
        for _, f in enumerate(uploaded_files):
            if f['Succeeded'] and file_name == f['FileName']:
                return
             
             
        raise QException(
        f"Job meta files failed to be uploaded. Assigned file name is '{file_name}'"
        )
    
    def _token_upload_to_cluster(self, encryption_password:str, job_info:SubmittedJobInfoExt):
        """Uploads python 
        
        :param enc_passw: Password to encrypt token with (original password, not encoded)
        :param job_info: Submitted job info
        :type SubmittedJobInfoExt:
        :raises QException: on upload failure

        """
        encrypted_token = encrypt_string(self._token, encryption_password)
        
        
        file_name = "user_token.enc"
        files = {'files': (file_name,
                                encrypted_token,
                                'text/plain')}

        full_upload_file_url = self._backend_metadata.lexis_resource.heappe_url + \
                QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={'Authorization': f"Bearer {self._token}"},
            timeout=120,
            params={'JobId': job_info.id, 'TaskId': job_info.tasks[0].id})

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        #pylint: disable=W0105
        """
        [
            {
                "FileName": "string",
                "Succeeded": true,
                "Path": "string"
            }
        ]
        """
        uploaded_files = upload_resp.json()
        # check if succeeded
        for _, f in enumerate(uploaded_files):
            if f['Succeeded'] and file_name == f['FileName']:
                return
             
             
        raise QException(
        f"Job meta files failed to be uploaded. Assigned file name is '{file_name}'"
        )
    
    @classmethod
    def _get_real_template_name(cls, project_name:str, template_name:str)->str:
        return project_name+"_"+template_name

    @property
    def heappe_client(self) -> Optional[HEAppEApi]:
        """
        Get the HEAppE client instance.

        :returns: HEAppE API client if initialized, None otherwise
        :rtype: Optional[HEAppEApi]
        """
        return self._heappe_client

    @property
    def is_authenticated(self) -> bool:
        """
        Check if client is fully authenticated.

        :returns: True if both LEXIS and HEAppE authentication completed successfully
        :rtype: bool
        """
        return self._authenticated and self._token is not None

    @property
    def lexis_project(self) -> str:
        """
        Get the LEXIS project identifier.

        :returns: LEXIS project name
        :rtype: str
        """
        return self._lexis_project

    def get_quantum_backend_info(self) -> QBackendMetadata:
        """
        Get quantum backend information and configuration.

        Retrieves information about the specified quantum backend, including
        connection details and authentication status. Currently returns basic
        configuration information.

        :param backend_name: Name of the quantum backend to query
        :type backend_name: str

        :returns: Dictionary containing backend information and status
        :rtype: Dict[str, Any]

        :raises QException: When backend information retrieval fails
        :raises QAuthException: When client authentication is invalid
        :raises QResultsFailed: When backend service communication fails

        .. note::
            This is a placeholder implementation that returns basic connection
            information. Production implementation would query actual quantum
            backend services for detailed capabilities and status.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        return self._backend_metadata

    def submit_quantum_job(self, job_data: Dict[str, Any], backend:IQMBackend|Pulla=None, circuits: SweepDefinition|str|list[str]|None = None, run_options: dict[str, Any]|None = None) -> int:
        """
        Submit a quantum job for execution via HEAppE.

        Creates and submits a quantum job specification to HEAppE infrastructure,
        configuring task parameters, environment variables, and resource requirements.
        Returns the HEAppE job ID for status monitoring and result retrieval.

        :param job_data: Job specification dictionary containing:

            * name (str): Job name identifier
            * walltime_limit (int): Maximum execution time in seconds
            * tasks (List[Dict]): Task specifications with template parameters
            * environment_variables (List[Dict]): Environment variable settings
            * project_id (int): HEAppE project ID (optional, uses default)
            * cluster_id (int): HEAppE cluster ID (optional, uses default)

        :type job_data: Dict[str, Any]
        
        :param circuits: Quantum circuit or list of circuits. OpenQASM serialized string.
        :type circuits: SweepDefinition|str|list[str]|None
        :param run_options: Dictionary of quantum circuit execution
        :type run_options: dict[str, Any]

        :returns: HEAppE job ID for monitoring and result retrieval
        :rtype: int

        :raises QException: When job creation or submission fails
        :raises QAuthException: When client authentication is invalid
        :raises QResultsFailed: When HEAppE job submission fails
        :raises ValueError: When job_data contains invalid parameters

        .. note::
            Current implementation uses fixed core allocation (16 min/max cores)
            for quantum tasks. Task parameters are extracted from job_data with
            fallbacks to configured defaults.

        Example:
            >>> job_data = {
            ...     'name': 'quantum_circuit_execution',
            ...     'walltime_limit': 1800,
            ...     'tasks': [{'template_parameter_values': [...]}],
            ...     'environment_variables': [{'name': 'Q_COMMAND', 'value': 'backend_run'}]
            ... }
            >>> job_id = client.submit_quantum_job(job_data)
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        if backend and isinstance(backend,Pulla) or (isinstance(circuits,list) and isinstance(circuits[0],SweepDefinition)):
            log.warning("We are sorry for inconvenience, Pulla is currently not supported. We are working on this feature")
            return NotImplemented

        # handle user token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        queue = 'qexecute' if backend else 'qinit'
        heappe_project_id = self._command_template_infos[queue]['target_project_id']
        heappe_cluster_id = self._command_template_infos[queue]['target_location_id']
        heappe_node_type_id = self._command_template_infos[queue]['target_node_type_id']
        heappe_command_template_id = self._command_template_infos[queue]['target_template_id']
        heappe_file_transfer_method_id = self._command_template_infos[queue]['target_node_type_file_transfer_method_id']
        minmax_cores = 1 if queue == 'qinit' else 2
        
        
        log.debug("LEXIS_PROJECT_RESOURCE_ID: %s", str(self._backend_metadata.lexis_resource.project_resource_id))
        
        try:
            env_variables = [
                *job_data.get("environment_variables", []),
                # For PULLA
                # EnvironmentVariableExt("Q_COMMAND","pulla_submit_playlist" if isinstance(circuits, SweepDefinition) else "backend_run")
                EnvironmentVariableExt("Q_COMMAND", "backend_run"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt("LEXIS_PROJECT_RESOURCE_ID", str(self._backend_metadata.lexis_resource.project_resource_id)),
                ]
            # Create job specification
            job_spec = JobSpecification(
                name=job_data.get('name', 'quantum_job'),
                project_id=job_data.get('project_id', heappe_project_id),
                cluster_id=job_data.get('cluster_id', heappe_cluster_id),
                tasks=[TaskSpecification(
                    name="quantum_task",
                    min_cores=minmax_cores,
                    max_cores=minmax_cores,
                    walltime_limit=job_data.get('walltime_limit', 7200),
                    progress_file="quantum_task_progress.log",
                    log_file="quantum_task_progress.log",
                    cluster_node_type_id=job_data.get('tasks', [{'cluster_node_type_id': heappe_node_type_id}])[
                        0].get('cluster_node_type_id', heappe_node_type_id),
                    command_template_id=job_data.get('command_template_id', [{'command_template_id': heappe_command_template_id}])[
                        0].get('command_template_id', heappe_command_template_id),
                    template_parameter_values=job_data.get('tasks', [{'template_parameter_values': []}])[
                        0].get('template_parameter_values', [])
                )],
                file_transfer_method_id=job_data.get(
                    'file_transfer_method_id', heappe_file_transfer_method_id),
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = heappe_job_management_api.heappe_job_management_create_job_post(
                    body=job_spec_model)
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to create job: {e.reason}; API status: {e.status}") from e
            
            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                upload_futures = []
                ####################
                # Upload circuits #
                ####################
                # if circuits is set, upload it to execution directory of job
                if circuits and isinstance(circuits,SweepDefinition) or (isinstance(circuits,list) and isinstance(circuits[0],SweepDefinition)):
                    upload_futures.append(executor.submit(self._python_object_upload_to_cluster, circuits, 'sweep', job_info, True))
                elif circuits:
                    q_circuits = circuits if isinstance(circuits, list) else [circuits]
                    for index, circuit in enumerate(q_circuits):
                        upload_futures.append(executor.submit(self._circuit_upload_to_cluster, circuit, f'circuit_{index}', job_info))
                        
                
                #####################
                # Upload run_kwargs #
                #####################
                # if run_kwargs is set, upload it to execution directory of job
                if run_options:
                    upload_futures.append(executor.submit(self._python_object_upload_to_cluster, run_options, "run_kwargs", job_info, True))
                
                ##################
                # Upload backend #
                ##################
                if backend and (isinstance(backend,IQMBackend)):
                    upload_futures.append(executor.submit(self._python_object_upload_to_cluster,backend, "backend", job_info, False))
                    # Fixes loading of iqm_ attrs of IQMTarget
                    if isinstance(backend,IQMBackend):
                        def _get_iqm_target_attrs(backend):
                            # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
                            return {
                                attr: getattr(backend.target, attr)
                                for attr in dir(backend.target)
                                if attr.startswith('iqm_') and not attr.startswith('__')
                            }
                        iqm_target_attrs = _get_iqm_target_attrs(backend)
                        upload_futures.append(executor.submit(self._python_object_upload_to_cluster, iqm_target_attrs, "iqm_target_attrs", job_info, False))
                elif backend and isinstance(backend,Pulla):
                    upload_futures.append(executor.submit(self._python_object_upload_to_cluster,backend, "pulla", job_info, True))

                done, pending = concurrent.futures.wait(upload_futures, return_when=concurrent.futures.FIRST_EXCEPTION)
                for future in pending:
                    future.cancel()
                
                # This will raise if any done future has an exception
                for future in done:
                    future.result()  # Raises exception if future failed
            
            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model)
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to submit job: {e.reason}; API status: {e.status}") from e
            return job_info.id

        except QException as e:
            raise e
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            raise QException("Job submission failed!!!") from e

    def get_job_status(self, job_id: int) -> Tuple[str, int, list[int]]:
        """
        Get current status of HEAppE job execution.

        Queries HEAppE for job status and maps internal job states to
        standardized status strings. Returns job status along with
        job and task identifiers for result retrieval.

        :param job_id: HEAppE job identifier
        :type job_id: int

        :returns: Tuple of (status_string, job_id, task_ids_list)
        :rtype: Tuple[str, int, list[int]]

        :raises QException: When status retrieval fails
        :raises QAuthException: When client authentication is invalid
        :raises QResultsFailed: When HEAppE status query fails
        :raises ValueError: When job_id is invalid

        **Status mapping:**

        * "FINISHED" - Job completed successfully
        * "WAITING" - Job queued or running
        * "FAILED" - Job failed or was canceled
        * "UNKNOWN" - Job status could not be determined

        Example:
            >>> status, job_id, task_ids = client.get_job_status(12345)
            >>> if status == "FINISHED":
            ...     results = client.get_job_results(job_id, task_ids)
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        try:

            heappe_job_management_api = JobManagementApi(self._heappe_client)
            try: 
                job_info: SubmittedJobInfoExt = heappe_job_management_api.heappe_job_management_current_info_for_job_get(
                    SubmittedJobInfoId=job_id
                )
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to get job info: {e.reason}; API status: {e.status}") from e

            if hasattr(job_info, 'state'):
                job_state = JobState(job_info.state)
                log.debug("Job '%d' state '%d', failed_code '%d', %s", job_id,
                          job_info.state, JobState.Failed.value, str(job_state == JobState.Failed))
                is_failed: bool = job_state == JobState.Failed or job_state == JobState.Canceled
                is_finished: bool = job_state.value > JobState.Running.value and job_state != JobState.WaitingForServiceAccount

                if is_failed:
                    return "FAILED", job_info.id, [task.id for task in job_info.tasks]
                if is_finished:
                    return "FINISHED", job_info.id, [task.id for task in job_info.tasks]
                return "WAITING", job_info.id, [task.id for task in job_info.tasks]
            else:
                return "UNKNOWN", 0, []

        except Exception as e:
            raise QException("Failed to get job status!") from e

    def get_job_results(self, job_id: int, file_names_to_fetch: list[str] = None, use_dill:list[bool] = None, job_status=None, task_ids=None, wait=False) -> Dict[str, Any]:
        """
        Retrieve quantum job execution results from HEAppE.

        Downloads and processes result files from completed HEAppE jobs,
        including pickled quantum results, circuit data, job information,
        and backend configurations. Handles job failure cases by retrieving
        stdout/stderr logs for debugging.

        :param job_id: HEAppE job identifier
        :type job_id: int
        :param file_names_to_fetch: List of specific files to download (uses defaults if None)
        :type file_names_to_fetch: list[str] or None
        :param use_dill: List of bools, should be same length as file_names_to_fetch. Defines whether file on same idx should be pickled using dill instead of pickle
        :param wait: If true, will wait until job is finished or failed

        :returns: Dictionary containing job results with keys:

            * results: Quantum measurement results
            * circuit: Circuit specification used
            * job: Job execution information  
            * backend: Backend configuration
            * status: Job completion status

        :rtype: Dict[str, Any] or None

        :raises QException: When result processing fails
        :raises QAuthException: When client authentication is invalid
        :raises QResultsFailed: When job execution failed or result retrieval failed

        **Return value:**

        * Returns None if job is not yet finished
        * Returns result dictionary if job completed successfully
        * Raises QResultsFailed with error logs if job failed

        .. note::
            Default files fetched: backend.pkl, job.pkl, results.pkl
            Failed jobs have their stdout/stderr included in QResultsFailed exception.

        Example:
            >>> results = client.get_job_results(12345)
            >>> if results:
            ...     counts = results['results'].get_counts()
            ...     print(f"Measurement counts: {counts}")
        """

        if not self.is_authenticated:
            raise QAuthException(
                reason="Client not authenticated",
                resource=f"job_{job_id}"
            )

        if file_names_to_fetch is None:
            file_names_to_fetch = []

        try:
            if not job_status or not task_ids:
                job_status, job_id, task_ids = self.get_job_status(job_id)
            log.debug("get_job_results status: %s", job_status)
            if wait:
                while job_status not in ["FINISHED", "FAILED"]:
                    time.sleep(QClient.DEFAULT_POLL_TIME)
                    job_status, _, _ = self.get_job_status(job_id)
            # Failed, try to find a reason
            if job_status == "FAILED":
                stderr_content = "None"
                stdout_content = "None"
                try:
                    file_content_stderr = self._download_file_from_cluster_binary(
                        submitted_job_context_id=job_id,
                        relative_file_path=f"/{job_id}/{task_ids[0]}/stderr.txt"
                    )

                    file_content_stdout = self._download_file_from_cluster_binary(
                        submitted_job_context_id=job_id,
                        relative_file_path=f"/{job_id}/{task_ids[0]}/stdout.txt"
                    )

                    # Unpickle the results
                    stdout_content = file_content_stdout.decode("utf-8")
                    stderr_content = file_content_stderr.decode("utf-8")
                except Exception as e:
                    raise QResultsFailed(job_id) from e
                raise QResultsFailed(job_id, "Failure reason (stderr):\n" +
                                     stderr_content+"\n-------\n (stdout):\n"+stdout_content)

            # Not finished, continue polling
            if job_status != "FINISHED":
                log.info("Job status: %s", job_status)
                return None

            # FINISHED

            # Look for results.pkl, job.pkl, and backend.pkl files created by the run script
            results_data = {}

            for idx, file_name in enumerate(file_names_to_fetch):
                try:
                    # Download the pickle file as binary
                    file_content = self._download_file_from_cluster_binary(
                        submitted_job_context_id=job_id,
                        relative_file_path=file_name
                    )

                    # Unpickle the results
                    unpickled_data = dill.loads(file_content) if use_dill[idx] else pickle.loads(file_content)
                    results_data[file_name.split("/")[-1].split(".")[0]] = unpickled_data

                except Exception as file_error:
                    log.warning(
                        "Could not download or unpickle %s: %s", file_name, file_error)
                    raise QException(
                        f"Could not download or unpickle {file_name}: ") from file_error

            return {
                **results_data,
                "status": "FINISHED"
            }

        except QException:
            raise
        except Exception as e:
            raise QResultsFailed(
                f"Failed to get job ({job_id}) results: {str(e)}"
            ) from e

    def get_pulla(self)->Tuple[Dict[str,Any],Pulla]:
        """Initialize Pulla and returns it data to be able to instantiate QPulla, to avoid calling API from client

        :raises QAuthException: Failed to verify client
        :raises QException: General exception raised inside QaaS
        :return: data required to initialize QPulla, Pulla instance created on remote
        """
        log.warning("We are sorry for inconvenience, Pulla is currently not supported. We are working on this feature")
        return NotImplemented
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        QUEUE = 'qinit'
        heappe_project_id = self._command_template_infos[QUEUE]['target_project_id']
        heappe_cluster_id = self._command_template_infos[QUEUE]['target_location_id']
        heappe_node_type_id = self._command_template_infos[QUEUE]['target_node_type_id']
        heappe_command_template_id = self._command_template_infos[QUEUE]['target_template_id']
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE]['target_node_type_file_transfer_method_id']
        MINMAX_CORES = 1
        try:
            env_variables = [
                EnvironmentVariableExt("Q_COMMAND","pulla_init"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt("LEXIS_PROJECT_RESOURCE_ID", str(self._backend_metadata.lexis_resource.project_resource_id)),
                ]
            # Create job specification
            job_spec = JobSpecification(
                name='quantum_pulla_init',
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[TaskSpecification(
                    name="quantum_task",
                    min_cores=MINMAX_CORES,
                    max_cores=MINMAX_CORES,
                    walltime_limit=60,
                    progress_file="quantum_task_progress.log",
                    log_file="quantum_task_progress.log",
                    cluster_node_type_id=heappe_node_type_id,
                    command_template_id=heappe_command_template_id,
                    template_parameter_values=[]
                )],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = heappe_job_management_api.heappe_job_management_create_job_post(
                    body=job_spec_model)
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to create job: {e.reason}; API status: {e.status}") from e
            
            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)
            
            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model)
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to submit job: {e.reason}; API status: {e.status}") from e
            
            # Fetch results of initialization
            results = self.get_job_results(job_info.id,
                                 [f"/{job_info.id}/{job_info.tasks[0].id}/pulla_data.pkl",
                                    f"/{job_info.id}/{job_info.tasks[0].id}/pulla.pkl"],
                                 use_dill=[True,True],
                                 task_ids=[job_info.tasks[0].id],
                                 wait=True
                                 )
            return results['pulla_data'], results['pulla']

        except QException as e:
            raise e
        except Exception as e:
            raise QException("Failed to initialize Pulla!!!") from e
        
    def get_calibration_set(self, calibration_set_id: UUID|None)->CalibrationSet:
        """Fetch from available calibration sets on QC

        :param calibration_set_id: When None, default calibration is fetched
        :raises QAuthException: _description_
        :raises QException: _description_
        :return: Calibration set
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        QUEUE = 'qinit'
        heappe_project_id = self._command_template_infos[QUEUE]['target_project_id']
        heappe_cluster_id = self._command_template_infos[QUEUE]['target_location_id']
        heappe_node_type_id = self._command_template_infos[QUEUE]['target_node_type_id']
        heappe_command_template_id = self._command_template_infos[QUEUE]['target_template_id']
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE]['target_node_type_file_transfer_method_id']
        MINMAX_CORES = 1
        try:
            env_variables = [
                                EnvironmentVariableExt("Q_COMMAND","get_calibration_set"),
                                EnvironmentVariableExt(encoded_pwd),
                                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                                EnvironmentVariableExt("LEXIS_PROJECT_RESOURCE_ID", str(self._backend_metadata.lexis_resource.project_resource_id)),
                                EnvironmentVariableExt("Q_OPTIONAL_ARG", str(calibration_set_id)),
                             ]
            # Create job specification
            job_spec = JobSpecification(
                name='quantum_get_calibration_set',
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[TaskSpecification(
                    name="quantum_task",
                    min_cores=MINMAX_CORES,
                    max_cores=MINMAX_CORES,
                    walltime_limit=60,
                    progress_file="quantum_task_progress.log",
                    log_file="quantum_task_progress.log",
                    cluster_node_type_id=heappe_node_type_id,
                    command_template_id=heappe_command_template_id,
                    template_parameter_values=[]
                )],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = heappe_job_management_api.heappe_job_management_create_job_post(
                    body=job_spec_model)
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to create job: {e.reason}; API status: {e.status}") from e
            
            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)
            
            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model)
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to submit job: {e.reason}; API status: {e.status}") from e
            
            # Fetch results of initialization
            results = self.get_job_results(job_info.id,
                                 [f"/{job_info.id}/{job_info.tasks[0].id}/calibration_set.pkl"],
                                 use_dill=[True],
                                 task_ids=[job_info.tasks[0].id],
                                 wait=True
                                 )
            return results['calibration_set']

        except QException as e:
            raise e
        except Exception as e:
            raise QException("Failed to fetch calibration set!!!") from e
    
    def get_dynamic_architecture(self, calibration_set_id: UUID|None=None)->DynamicQuantumArchitecture:
        """Fetch from available calibration sets on QC

        :param calibration_set_id: When None, default calibration is fetched
        :raises QAuthException: _description_
        :raises QException: _description_
        :return: Calibration set
        """
        _calibration_set_id = calibration_set_id if calibration_set_id is not None else "default"
        
        if _calibration_set_id in self._dynamic_quantum_architectures:
            return self._dynamic_quantum_architectures[_calibration_set_id]
        
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)
        
        # Determine whether to select qinit or qexecute
        QUEUE = 'qinit'
        heappe_project_id = self._command_template_infos[QUEUE]['target_project_id']
        heappe_cluster_id = self._command_template_infos[QUEUE]['target_location_id']
        heappe_node_type_id = self._command_template_infos[QUEUE]['target_node_type_id']
        heappe_command_template_id = self._command_template_infos[QUEUE]['target_template_id']
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE]['target_node_type_file_transfer_method_id']
        MINMAX_CORES = 1
        try:
            env_variables = [
                                EnvironmentVariableExt("Q_COMMAND","get_dynamic_quantum_architecture"),
                                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                                EnvironmentVariableExt("LEXIS_PROJECT_RESOURCE_ID", str(self._backend_metadata.lexis_resource.project_resource_id)),
                                EnvironmentVariableExt("Q_OPTIONAL_ARG", str(_calibration_set_id)),
                             ]
            # Create job specification
            job_spec = JobSpecification(
                name='quantum_get_dynamic_architecture',
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[TaskSpecification(
                    name="quantum_task",
                    min_cores=MINMAX_CORES,
                    max_cores=MINMAX_CORES,
                    walltime_limit=60,
                    progress_file="quantum_task_progress.log",
                    log_file="quantum_task_progress.log",
                    cluster_node_type_id=heappe_node_type_id,
                    command_template_id=heappe_command_template_id,
                    template_parameter_values=[]
                )],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = heappe_job_management_api.heappe_job_management_create_job_post(
                    body=job_spec_model)
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to create job: {e.reason}; API status: {e.status}") from e
            
            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)
            
            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model)
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to submit job: {e.reason}; API status: {e.status}") from e
            
            # Fetch results of initialization
            results = self.get_job_results(job_info.id,
                                 [f"/{job_info.id}/{job_info.tasks[0].id}/dynamic_quantum_architecture.pkl"],
                                 use_dill=[True],
                                 task_ids=[job_info.tasks[0].id],
                                 wait=True
                                 )
            dynamic_quantum_architecture = results['dynamic_quantum_architecture']
            self._dynamic_quantum_architectures[dynamic_quantum_architecture.calibration_set_id] = (
            dynamic_quantum_architecture
            )
            
            return dynamic_quantum_architecture

        except QException as e:
            raise e
        except Exception as e:
            raise QException("Failed to fetch calibration set!!!") from e
    
    def get_available_backends(self)->Dict[str, Any]:
        """ Get available backends based on UserOrganization Resources and Assignments information
        List location and aggregation names with LocationTypeId == 7 (Quantum) and their associated resource names, which can be used to determine available quantum backends and their configurations.
        """
        if not self._authenticated:
            raise QAuthException("Unauthorized!!!")

        try:
            headers = {
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json'
            }

            # Get project resources
            project_resources_url = f"{self._lexis_userorg_api_url}/api/ProjectResource"
            response = requests.get(project_resources_url, headers=headers, params={
                                    'ProjectShortName': self._lexis_project}, timeout=30)
            response.raise_for_status()

            project_resources = response.json()

            log.debug("Project resource: %s", str(project_resources))

            # Find the project by name or ID
            backend_metadata_list: list[QBackendMetadata] = []
            for project_resource_info in project_resources:
                for assignment_info in project_resource_info.get('Assignments', []):
                    location_type_id = project_resource_info.get('LocationTypeId')
                    if location_type_id == 7:  # LocationTypeId 7 corresponds to locations in LEXIS, which we use for quantum backends
                        aggregation_name = project_resource_info.get('AggregationName')
                        if aggregation_name in QClient.DEFAULT_LEXIS_AGGREGATION_NAME:
                            
                            quantum_technology = "UNKNOWN"
                            sw_stack = "UNKNOWN"
                            heappe_url = None
                            for spec in assignment_info.get('Specifications', []):
                                if spec.get('Key') == 'HEAPPE_URL':
                                    heappe_url = spec.get('Value')
                                elif spec.get('Key') == 'SW_STACK':
                                    sw_stack = spec.get('Value')
                                elif spec.get('Key') == 'QUANTUM_TECHNOLOGY':
                                    quantum_technology = spec.get('Value')
                            
                            qmetadata = QBackendMetadata(
                            backend_name=assignment_info["AggregationName"],
                            swstack=sw_stack,
                            available="UNKNOWN", #FIXME: get this information
                            quantum_technology=quantum_technology,
                            lexis_resource=LexisResource(
                                project_resource_info["Name"],
                                assignment_info["AllocationAmount"],
                                assignment_info["ProjectResourceId"],
                                project_resource_info["StartDate"],
                                project_resource_info["EndDate"],
                                heappe_url
                                ),
                            lexis_project=LexisProject(self._lexis_project),
                            host_entity=assignment_info["LocationName"]
                            )
                            backend_metadata_list.append(qmetadata)
            return {metadata.backend_name: metadata for metadata in backend_metadata_list}
        except:
            raise QAuthException(
                reason=f"Failed to retrieve resources for LEXIS project '{self._lexis_project}', please verify your assignment and try again!",
                user_id=self._username
            )
    
    def _download_file_from_cluster_binary(self,
                                           submitted_job_context_id: int,
                                           relative_file_path: str) -> bytes:
        """
        Download file from HEAppE cluster as binary data.

        Internal method for downloading files from completed HEAppE jobs,
        handling base64 decoding of file content returned by HEAppE API.

        :param submitted_job_context_id: HEAppE job ID for file location
        :type submitted_job_context_id: int
        :param relative_file_path: Path to file within job directory
        :type relative_file_path: str

        :returns: Binary file content
        :rtype: bytes

        :raises QException: When file download fails
        :raises QAuthException: When session authentication is invalid
        :raises QResultsFailed: When HEAppE file transfer fails

        .. note::
            This method handles the HEAppE-specific file transfer protocol
            including base64 encoding/decoding of binary file content.
        """

        download_body = DownloadFileFromClusterModel(
            submitted_job_info_id=submitted_job_context_id,
            relative_file_path=relative_file_path
        )

        api_request_body = {
            "_preload_content": False,
            "body": download_body
        }

        file_transfer_api = FileTransferApi(self.heappe_client)
        try:
            response = file_transfer_api.heappe_file_transfer_download_file_from_cluster_post(
                **api_request_body)
        except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(f"Unable to download file from cluster: {e.reason}; API status: {e.status}") from e
        return base64.b64decode(json.loads(response.data))

    def _get_command_template_ids(self, template_name_qinit: str, template_name_qexecute: str, qinit_queue_name:str, qexecute_queue_name:str) -> Dict[str,Dict[str,int]]:
        """
        Retrieve HEAppE command template configuration for quantum jobs.

        Queries HEAppE cluster information to find the appropriate command
        template for quantum job execution, along with associated cluster,
        node type, project, and file transfer method identifiers.

        :param template_name: Specific template name (uses DEFAULT_TEMPLATE_NAME if None)
        :type template_name: str or None

        :returns: Tuple of (cluster_id, node_type_id, project_id, template_id, file_transfer_method_id)
        :rtype: Tuple[int, int, int, int, int]

        :raises QException: When template lookup fails or configuration is invalid
        :raises QAuthException: When cluster information access is denied
        :raises QResultsFailed: When HEAppE cluster information retrieval fails

        .. note::
            Searches for template within the quantum location and project
            resource specified during client initialization.
        """

        qinit_template_name = QClient._get_real_template_name(self._lexis_project, template_name_qinit)
        qexecute_template_name = QClient._get_real_template_name(self._lexis_project, template_name_qexecute)

        def get_command_template_call(target_template_name):
            try:
                # This would typically call HEAppE API to list command templates
                # For now, using a placeholder implementation
                heappe_cluster_info_api = ClusterInformationApi(
                    self._heappe_client)

                # Get available command templates (API endpoint may vary)
                clusters: ClusterExt = heappe_cluster_info_api.heappe_cluster_information_list_available_clusters_get(
                    ClusterName=self._backend_metadata.backend_name,
                    AccountingString=[self._backend_metadata.lexis_resource.resource_name],
                    CommandTemplateName=target_template_name
                )
            except Exception as e:
                log.warning("Failed to get command template ID")
                raise QException("Failed to get command template ID") from e

            log.debug("Available clusters from HEAppE: %s", str(clusters))

            target_location: ClusterExt = None
            target_node_type: ClusterNodeTypeExt = None
            target_project: ProjectExt = None
            target_template: CommandTemplateExt = None

            for cluster in clusters:
                if cluster.name == self._backend_metadata.backend_name:
                    target_location = cluster
                    break

            if not target_location:
                raise QException(
                    f"Quantum location '{self._backend_metadata.backend_name}' not found in HEAppE")

            # Fetch information about node type (partition), cluster etc.
            log.debug("Queue names to be searched: %s, %s", qinit_queue_name, qexecute_queue_name)
            log.debug("Template names to be searched: %s, %s", qinit_template_name, qexecute_template_name)


            for node_type_in_location in target_location.node_types:
                if node_type_in_location.name in [qinit_queue_name, qexecute_queue_name]:
                    for project_in_node_type in node_type_in_location.projects:
                        if project_in_node_type.accounting_string == self._backend_metadata.lexis_resource.resource_name:
                            target_node_type = node_type_in_location
                            target_project = project_in_node_type
                            break
                if target_project:
                    break

            if not target_project:
                log.warning(f"Verify all command templates are accessible to your user for Project '{self._backend_metadata.lexis_resource.resource_name}'!")
                
                raise QException(
                    f"Project '{self._backend_metadata.lexis_resource.resource_name}' in HEAppE not found")
            log.debug("target_project: %s",str(target_project))
            log.debug("target_project.command_templates: %s",str(target_project.command_templates))
            for template in target_project.command_templates:
                if template.name == target_template_name:
                    target_template = template
                    break

            if not target_template:
                raise QException(
                    f"Command template '{target_template_name}' not found in HEAppE")
            return {
                'target_location_id': target_location.id,
                'target_node_type_id': target_node_type.id,
                'target_project_id': target_project.id,
                'target_template_id': target_template.id,
                'target_node_type_file_transfer_method_id': target_node_type.file_transfer_method_id,
                'qinit_queue_name': qinit_queue_name,
                'qexecute_queue_name': qexecute_queue_name
            }
        template_infos = {
            'qinit': get_command_template_call(qinit_template_name),
            'qexecute': get_command_template_call(qexecute_template_name)
        }

        return template_infos

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a running or queued HEAppE job.

        Attempts to cancel the specified job through HEAppE job management API.
        Returns success status but does not raise exceptions on failure.

        :param job_id: HEAppE job identifier to cancel
        :type job_id: str

        :returns: True if cancellation succeeded, False otherwise
        :rtype: bool

        :raises QAuthException: When client authentication is invalid        
        .. note::
            This method converts job_id to integer internally for HEAppE API.
            Cancellation may not be possible for jobs that are already running
            or completed.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        try:

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            cancel_job_model = CancelJobModel(submitted_job_info_id=int(job_id))

            heappe_job_management_api.heappe_job_management_cancel_job_put(
                body=cancel_job_model
            )
            return True
        except ApiException as e:
            if e.status == 401:
                raise QAuthException("Unauthorized!!!") from e
            raise QException(f"Unable to cancel job: {e.reason}; API status: {e.status}") from e
        except Exception as e:  # pylint: disable=W0718
            log.error("Error cancelling job: %s", str(e))
            return False

    def close_session(self):
        """
        Close the HEAppE session and clean up client state.

        Terminates the HEAppE session and resets client authentication state.
        Should be called when the client is no longer needed to free resources.

        :raises QException: When session cleanup fails
        :raises QAuthException: When session termination encounters errors
        :raises QResultsFailed: When HEAppE session closure fails

        .. note::
            This method is automatically called when using QClient as a
            context manager (with statement).
        """
        if self._heappe_client:
            try:
                self._authenticated = False
                self._heappe_client = None
            except Exception as e:
                log.error("Error closing session: %s", str(e))
                raise QAuthException("Error closing exception") from e

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close session"""
        self.close_session()
