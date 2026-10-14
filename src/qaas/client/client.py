import base64
import concurrent
import json
import logging
import os
import pickle
import ssl
import sys
import time
import urllib
import warnings
from datetime import UTC, datetime
from importlib import metadata as m
from typing import Any
from uuid import UUID

import dill
import jwt
import requests
import truststore
from iqm.iqm_server_client.models import CalibrationSet
from iqm.pulla.pulla import Pulla
from iqm.qiskit_iqm import IQMBackend
from iqm.station_control.interface.models import (
    DynamicQuantumArchitecture,
    RunDefinition,
)
from jwt import PyJWKClient
from py4heappe.heappe_v6.core import (
    ApiClient as HEAppEApi,
)
from py4heappe.heappe_v6.core import (
    Configuration as HEAppEConfiguration,
)
from py4heappe.heappe_v6.core.api import (
    ClusterInformationApi,
    FileTransferApi,
    JobManagementApi,
)
from py4heappe.heappe_v6.core.models import (
    CancelJobModel,
    ClusterExt,
    ClusterNodeTypeExt,
    CommandTemplateExt,
    CreateJobByProjectModel,
    DownloadFileFromClusterModel,
    EnvironmentVariableExt,
    ProjectExt,
    SubmitJobModel,
    SubmittedJobInfoExt,
)
from py4heappe.heappe_v6.core.models import (
    JobSpecificationExt as JobSpecification,
)
from py4heappe.heappe_v6.core.models import (
    TaskSpecificationExt as TaskSpecification,
)
from py4heappe.heappe_v6.core.rest import ApiException

from .backend_metadata import LexisProject, LexisResource, QBackendMetadata
from .cryption_control import encrypt_string, generate_password
from .utils import JobState, QAuthException, QException, QResultsFailed

# -----------
# Set Logging
# -----------

log = logging.getLoggerClass()(
    __name__, os.environ.get("QPROVIDER_LOGLEVEL", "INFO").upper()
)

# Formatter for consistent output
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# Decide handler: file or stderr
logfile = os.environ.get("QPROVIDER_LOGFILE", None)
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
    Client for quantum backends exposed through LEXIS and HEAppE.

    The client authenticates the user against LEXIS (JWT + project membership),
    resolves the quantum resource assigned to the project, locates the HEAppE
    instance serving that resource and the command templates used to run work
    on it. Every remote operation (circuit execution, Pulla initialisation,
    calibration / architecture queries) is executed as a HEAppE job.

    Two HEAppE queues are used:

    * ``qinit`` -- short, lightweight jobs (metadata queries, Pulla init).
    * ``qexecute`` -- jobs that actually run on the quantum computer.

    The user's JWT is forwarded to each job encrypted with a per-job random
    password; the password itself is passed via the ``USER_JWT_PWD``
    environment variable.

    :param token: LEXIS JWT access token.
    :type token: str
    :param lexis_project: LEXIS project short name.
    :type lexis_project: str
    :param lexis_resource_name: Name of the LEXIS project resource to use.
        Auto-detected when ``None``.
    :type lexis_resource_name: str or None
    :param quantum_computer_name: Quantum computer within the resource (equal to
        ``AggregationName`` in LEXIS resource assignments). Auto-detected when ``None``.
    :type quantum_computer_name: str or None
    :param provider_token: Additional provider-specific token (e.g. vendor API token).
    :type provider_token: str or None
    :param kwargs: Optional overrides, see :meth:`__init__`.

    :raises QAuthException: Authentication or authorization failed.
    :raises QException: Configuration or HEAppE lookup failed.

    :cvar DEFAULT_QINIT_TEMPLATE_NAME: HEAppE command template for the ``qinit`` queue.
    :vartype DEFAULT_QINIT_TEMPLATE_NAME: str
    :cvar DEFAULT_QEXECUTE_TEMPLATE_NAME: HEAppE command template for the ``qexecute`` queue.
    :vartype DEFAULT_QEXECUTE_TEMPLATE_NAME: str
    :cvar DEFAULT_QINIT_QUEUE_NAME: HEAppE node type (queue) name for init jobs.
    :vartype DEFAULT_QINIT_QUEUE_NAME: str
    :cvar DEFAULT_QEXECUTE_QUEUE_NAME: HEAppE node type (queue) name for execution jobs.
    :vartype DEFAULT_QEXECUTE_QUEUE_NAME: str
    :cvar DEFAULT_USERORG_BASE_URL: Base URL of the LEXIS UserOrg API.
    :vartype DEFAULT_USERORG_BASE_URL: str
    :cvar DEFAULT_QUANTUM_LOCATION_TYPE: LEXIS ``LocationTypeId`` denoting quantum locations.
    :vartype DEFAULT_QUANTUM_LOCATION_TYPE: int
    :cvar UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT: HEAppE 6.2 upload endpoint not yet
        covered by ``py4heappe``.
    :vartype UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT: str
    :cvar DEFAULT_POLL_TIME: Polling interval in seconds for job status checks.
    :vartype DEFAULT_POLL_TIME: float
    :cvar TERMINAL_JOB_STATES: Statuses returned by :meth:`get_job_status` that a job
        never leaves.
    :vartype TERMINAL_JOB_STATES: tuple[str, ...]

    .. note::
        All authentication and resource resolution happens in the constructor,
        which therefore performs several network round trips.

    Example::

        with QClient(token, "my_project") as client:
            info = client.get_quantum_backend_info()
            job_id = client.submit_quantum_job({"name": "bell"}, backend, qasm)
            results = client.get_job_results(job_id, ["..."], use_dill=[False], wait=True)
    """

    # Two templates for two different queues are required by HEAppE architecture
    DEFAULT_QINIT_TEMPLATE_NAME = "RunQInit"
    DEFAULT_QEXECUTE_TEMPLATE_NAME = "RunQExecute"
    DEFAULT_QINIT_QUEUE_NAME = "init_queue"
    DEFAULT_QEXECUTE_QUEUE_NAME = "compute_queue"
    DEFAULT_USERORG_BASE_URL = "https://api.lexis.tech/userorg"

    DEFAULT_QUANTUM_LOCATION_TYPE = 7

    # HEAppE 6.2 endpoints (not yet in client)
    UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT = (
        "/heappe/FileTransfer/UploadFilesToJobExecutionDir"
    )

    DEFAULT_POLL_TIME = 0.5

    # Statuses from :meth:`get_job_status` that a job never transitions out of. Poll loops
    # must stop on all of them, or a canceled/failed job spins forever.
    TERMINAL_JOB_STATES = ("FINISHED", "FAILED", "CANCELED")

    def __init__(
        self,
        token: str,
        lexis_project: str,
        lexis_resource_name: str | None = None,
        quantum_computer_name: str | None = None,
        provider_token: str | None = None,
        **kwargs,
    ):
        """
        Authenticate the user and resolve the quantum backend.

        Steps performed, all of which must succeed:

        #. Verify the JWT and LEXIS project membership/activity
           (:meth:`_authenticate_authorize_lexis`).
        #. Resolve the quantum resource and its HEAppE URL
           (:meth:`_authorize_lexis_resource`).
        #. Check client/server ``qaas`` version compatibility
           (:meth:`_verify_backend_version`).
        #. Create the HEAppE API client (:meth:`_authenticate_heappe`).
        #. Resolve HEAppE command templates for both queues
           (:meth:`_get_command_template_ids`).

        :param token: LEXIS JWT access token.
        :type token: str
        :param lexis_project: LEXIS project short name.
        :type lexis_project: str
        :param lexis_resource_name: LEXIS project resource name; auto-detected when ``None``.
        :type lexis_resource_name: str or None
        :param quantum_computer_name: Quantum computer (``AggregationName``) within
            the resource; auto-detected when ``None``.
        :type quantum_computer_name: str or None
        :param provider_token: Additional provider-specific token.
        :type provider_token: str or None
        :keyword str lexis_userorg_api_url: Override of :attr:`DEFAULT_USERORG_BASE_URL`.
        :keyword list[str] lexis_aggregation_name: Accepted aggregation names
            (default ``["VLQ", "EQE1", "QLM"]``).
        :keyword str qinit_command_template_name: Override of :attr:`DEFAULT_QINIT_TEMPLATE_NAME`.
        :keyword str qexecute_command_template_name: Override of
            :attr:`DEFAULT_QEXECUTE_TEMPLATE_NAME`.
        :keyword str qinit_queue_name: Override of :attr:`DEFAULT_QINIT_QUEUE_NAME`.
        :keyword str qexecute_queue_name: Override of :attr:`DEFAULT_QEXECUTE_QUEUE_NAME`.
        :keyword str cmd_socket_path: Development only; forwarded to jobs as
            ``IQM_SERVICE_SOCKET``.

        :raises QAuthException: Token, project or resource authorization failed.
        :raises QException: Version mismatch or HEAppE template lookup failed.
        """

        # Constants
        self.DEFAULT_LEXIS_AGGREGATION_NAME = ["VLQ", "EQE1", "QLM"]

        self._token = token
        self._lexis_project = lexis_project
        self.provider_token = provider_token
        self._heappe_client: HEAppEApi | None = None
        # Caution, this attribute is changed by all authentication functions during initialization
        self._authenticated = (
            False  # FIXME: check that all login flows are correctly handled
        )

        # These will be populated during resource authorization
        self._project_id = None
        self._cluster_id = None

        # Operational kwargs
        self._lexis_userorg_api_url = kwargs.get(
            "lexis_userorg_api_url", self.DEFAULT_USERORG_BASE_URL
        )
        self._lexis_aggregation_name = kwargs.get(
            "lexis_aggregation_name",
            self.DEFAULT_LEXIS_AGGREGATION_NAME,  # list[str]
        )

        # Authenticate on initialization
        self._username, self._lexis_project_info = self._authenticate_authorize_lexis()
        self._authenticated = True

        self._backend_metadata = self._authorize_lexis_resource(
            lexis_resource_name, quantum_computer_name
        )
        self._verify_backend_version()
        self._heappe_client = self._authenticate_heappe()
        self._command_template_infos = self._get_command_template_ids(
            template_name_qinit=kwargs.get(
                "qinit_command_template_name", self.DEFAULT_QINIT_TEMPLATE_NAME
            ),
            template_name_qexecute=kwargs.get(
                "qexecute_command_template_name", self.DEFAULT_QEXECUTE_TEMPLATE_NAME
            ),
            qinit_queue_name=kwargs.get(
                "qinit_queue_name", self.DEFAULT_QINIT_QUEUE_NAME
            ),
            qexecute_queue_name=kwargs.get(
                "qexecute_queue_name", self.DEFAULT_QEXECUTE_QUEUE_NAME
            ),
        )

        # Socket path (dev)
        self._socket_path = kwargs.get("cmd_socket_path", None)

        # Architecture
        self._dynamic_quantum_architectures = {}

    def _verify_backend_version(self):
        """
        Check that the installed ``qaas`` package matches the server version.

        Versions are compared as semantic versions (an optional leading ``v`` is
        ignored):

        * major or minor mismatch -- blocking, raises :class:`QException`;
        * patch mismatch -- non-blocking, emits a :class:`UserWarning`.

        :raises QException: Major or minor version differs.
        :raises ValueError: A version string is not a valid ``X.Y.Z`` version.
        """

        def _parse_semver(v: str):
            v = (v or "").strip()
            if v.startswith(("v", "V")):
                v = v[1:]
            parts = v.split(".")
            if len(parts) < 3:
                raise ValueError(f"Invalid semantic version: {v!r}")
            return tuple(int(p) for p in parts[:3])

        def validate_qaas_version(client_version: str, server_version: str):
            """
            Major or Minor mismatch => raise QException (blocking)
            Patch mismatch => warn (non-blocking)
            """
            exp_major, exp_minor, exp_patch = _parse_semver(client_version)
            act_major, act_minor, act_patch = _parse_semver(server_version)

            if act_major != exp_major or act_minor != exp_minor:
                raise QException(
                    message=(
                        "Major or Minor Version Mismatch: client must be updated. "
                        f"(expected={client_version}, actual={server_version})."
                    )
                )

            if act_patch != exp_patch:
                warnings.warn(
                    "Patch Version Mismatch: recommending update "
                    f"(expected={client_version}, actual={server_version}).",
                    category=UserWarning,
                )

        # Get version from current package "qaas"
        validate_qaas_version(
            client_version=m.version("qaas"),
            server_version=self._backend_metadata.qaas_version,
        )

    def _authenticate_authorize_lexis(self) -> tuple[str, dict[str, Any]]:
        """
        Verify the JWT and the user's membership in the LEXIS project.

        Performed checks:

        * JWT signature, using the JWKS advertised by the issuer's
          ``/.well-known/openid-configuration``;
        * issuer (``iss``) and expiration (``exp``);
        * presence of a user identifier (``preferred_username`` or ``sub``);
        * project membership via the UserOrg ``/api/Project`` endpoint;
        * project activity window (``StartDate`` / ``EndDate``), when present.

        On Windows the OS trust store is used for TLS (via ``truststore``).

        :returns: ``(user_id, project_info)`` where ``project_info`` is the raw
            UserOrg project record.
        :rtype: tuple[str, dict[str, Any]]

        :raises QAuthException: Any of the checks above failed.
        """

        try:
            ############################################################
            # Extract issuer from JWT token without verification first #
            ############################################################
            # This allows us to get the Keycloak base URL dynamically
            unverified_token = jwt.decode(
                self._token, options={"verify_signature": False}
            )
            keycloak_base_url = unverified_token.get("iss")

            if not keycloak_base_url:
                raise ValueError("JWT token missing issuer (iss) claim")

            log.debug("Detected JWT issuer: %s", keycloak_base_url)

            # Initialize SSL context before fetching discovery metadata
            if sys.platform == "win32":
                ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            else:
                ctx = ssl.create_default_context()

            # Fetch OpenID Configuration dynamically
            openid_config_url = (
                f"{keycloak_base_url.rstrip('/')}/.well-known/openid-configuration"
            )
            log.debug("Fetching OpenID configuration from: %s", openid_config_url)

            req = urllib.request.Request(openid_config_url)
            with urllib.request.urlopen(req, context=ctx) as response:
                openid_config = json.loads(response.read().decode("utf-8"))

            jwks_url = openid_config.get("jwks_uri")
            if not jwks_url:
                raise ValueError("OpenID configuration response is missing 'jwks_uri'")

            log.debug("JWKS: %s", str(jwks_url))

            # Initialize JWKS client
            jwks_client = PyJWKClient(jwks_url, ssl_context=ctx)

            # Get signing key from JWT header
            signing_key = jwks_client.get_signing_key_from_jwt(self._token)

            # Read the token's unverified header to identify the algorithm and key ID
            token_header = jwt.get_unverified_header(self._token)
            token_alg = token_header.get("alg")

            if not token_alg:
                raise ValueError("JWT token header is missing the 'alg' parameter")

            # Now decode and fully verify JWT token
            decoded_token = jwt.decode(
                self._token,
                signing_key.key,
                algorithms=[token_alg],
                # audience="lexis-portal",  # Can be extracted from token if needed
                issuer=keycloak_base_url,
                audience="portal",
                # Removed verify_aud for flexibility
                options={"verify_exp": True, "verify_iss": True, "verify_aud": False},
            )

            # Extract user information from token
            user_id = decoded_token.get("preferred_username") or decoded_token.get(
                "sub"
            )

            if not user_id:
                raise QAuthException(
                    reason="Invalid token: missing user information",
                    user_id=user_id,
                    resource=self._lexis_project,
                )

            # Check token expiration
            exp_timestamp = decoded_token.get("exp")
            if exp_timestamp and exp_timestamp < time.time():
                raise QAuthException(
                    reason="Token has expired",
                    user_id=user_id,
                    resource=self._lexis_project,
                )

            log.debug("JWT token validated successfully for user: %s", user_id)

        except Exception as e:
            log.error(e)
            raise QAuthException(resource=self._lexis_project) from e

        try:
            ##############################################
            # Check project membership using UserOrg API #
            ##############################################

            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }

            # Get user's project memberships
            user_projects_url = f"{self._lexis_userorg_api_url}/api/Project"

            response = requests.get(
                user_projects_url,
                headers=headers,
                params={"ProjectShortName": self._lexis_project},
                timeout=30,
            )
            response.raise_for_status()

            projects_data = response.json()

            # Check if user is member of the specified LEXIS project
            project_found = False
            project_info = None

            for project in projects_data:
                # Check both 'id' and 'name' fields for project identification
                project_shortname = project.get("ShortName")

                if project_shortname == self._lexis_project:
                    project_found = True
                    project_info = project
                    break

            if not project_found:
                available_projects = set()
                for proj in projects_data:
                    proj_identifier = proj.get("ShortName")
                    if proj_identifier:
                        available_projects.add(proj_identifier)

                raise QAuthException(
                    reason=f"User does not have access to LEXIS project '{self._lexis_project}'. Available projects: {available_projects}",
                    user_id=user_id,
                    resource=self._lexis_project,
                )

            log.debug("Project access verified: %s", self._lexis_project)
            if project_info:
                log.debug("Project details: %s ", str(project_info))

        except Exception as e:
            raise QAuthException(user_id=user_id, resource=self._lexis_project) from e

        ###############################################
        # Verify project is active/enabled #
        ###############################################
        project_start_date = project_info.get("StartDate", None)
        project_end_date = project_info.get("EndDate", None)

        if project_start_date and project_end_date:
            try:
                # Parse ISO format dates
                start_dt = datetime.fromisoformat(project_start_date).replace(
                    tzinfo=UTC
                )
                end_dt = datetime.fromisoformat(project_end_date).replace(tzinfo=UTC)
                current_dt = (
                    datetime.now(start_dt.tzinfo)
                    if start_dt.tzinfo
                    else datetime.now(UTC)
                )

                if current_dt < start_dt:
                    raise QAuthException(
                        reason=f"Project '{self._lexis_project}' has not started yet. Start date: {project_start_date}",
                        user_id=user_id,
                        resource=self._lexis_project,
                    )
                elif current_dt > end_dt:
                    raise QAuthException(
                        reason=f"Project '{self._lexis_project}' has expired. End date: {project_end_date}",
                        user_id=user_id,
                        resource=self._lexis_project,
                    )
                else:
                    log.debug(
                        "Project is active from %s to %s",
                        project_start_date,
                        project_end_date,
                    )

            except ValueError as date_error:
                log.warning("Could not parse project dates: %s", date_error)
                raise QAuthException("Could not parse project dates") from date_error

        return user_id, project_info

    def _authorize_lexis_resource(
        self,
        lexis_resource_name: str | None = None,
        quantum_computer_name: str | None = None,
    ) -> QBackendMetadata:
        """
        Resolve the quantum resource of the project and build backend metadata.

        Resources are read from the UserOrg ``/api/ProjectResource`` endpoint.

        * If ``lexis_resource_name`` is ``None``, the first resource with
          ``LocationTypeId ==`` :attr:`DEFAULT_QUANTUM_LOCATION_TYPE` whose
          ``AggregationName`` is in the accepted aggregation names (and equals
          ``quantum_computer_name``, if given) is selected.
        * Otherwise the resource with the given name is selected, together with
          its first assignment of an accepted aggregation name.

        For the selected resource the activity window is verified, the
        ``HEAPPE_URL`` specification is read, and ``<HEAPPE_URL>/qaas`` is
        queried for ``qaas_version``, ``sw_stack`` (both required) and
        ``quantum_technology`` (optional).

        :param lexis_resource_name: Resource name; auto-detected when ``None``.
        :type lexis_resource_name: str or None
        :param quantum_computer_name: ``AggregationName`` to match during
            auto-detection; any accepted name when ``None``.
        :type quantum_computer_name: str or None

        :returns: Metadata of the resolved backend.
        :rtype: QBackendMetadata

        :raises QAuthException: Client not authenticated, resource/assignment not
            found, resource inactive, or required specification missing.
        """

        if not self._authenticated:
            raise QAuthException("Unauthorized!!!")

        try:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }

            # Get project resources
            project_resources_url = f"{self._lexis_userorg_api_url}/api/ProjectResource"
            response = requests.get(
                project_resources_url,
                headers=headers,
                params={"ProjectShortName": self._lexis_project},
                timeout=30,
            )
            response.raise_for_status()

            project_resources = response.json()

            log.debug("Project resource: %s", str(project_resources))

            # Find the project by name or ID
            project_resource_info = None
            assignment_info = None
            if lexis_resource_name is None:  # Try to find automatically
                for resource in project_resources:
                    for assignment in resource.get("Assignments", []):
                        location_type_id = resource.get("LocationTypeId")
                        if (
                            location_type_id == QClient.DEFAULT_QUANTUM_LOCATION_TYPE
                        ):  # LocationTypeId 7 corresponds to locations in LEXIS, which we use for quantum backends
                            aggregation_name = resource.get("AggregationName")
                            if aggregation_name in self._lexis_aggregation_name and (
                                quantum_computer_name is None
                                or quantum_computer_name == aggregation_name
                            ):
                                assignment_info = assignment
                                project_resource_info = resource
                                break
                    if assignment_info and project_resource_info:
                        break

                if not project_resource_info:
                    raise QAuthException(
                        reason=f"No resource with VLQ found for LEXIS project '{self._lexis_project}', please verify you have assigned one!",
                        user_id=self._username,
                    )
            else:
                for resource in project_resources:
                    resource_name = resource.get("Name")
                    log.debug("Project resource_name: %s", str(resource_name))
                    if resource_name == lexis_resource_name:
                        project_resource_info = resource
                        for assignment in resource.get("Assignments", []):
                            location_name = assignment.get("AggregationName")
                            log.debug("Project location_name: %s", str(location_name))
                            if location_name in self._lexis_aggregation_name:
                                assignment_info = assignment
                                break
                        break

            if not project_resource_info or not assignment_info:
                raise QAuthException(
                    reason=f"Resource or assignment of type {self._lexis_aggregation_name} not found in available resources for project '{self._lexis_project}'",
                    user_id=self._username,
                    resource=lexis_resource_name,
                )

            ###############################################
            # Verify resource is active/enabled #
            ###############################################
            resource_start_date = project_resource_info.get("StartDate", None)
            resource_end_date = project_resource_info.get("EndDate", None)

            if resource_start_date and resource_end_date:
                try:
                    # Parse ISO format dates
                    start_dt = datetime.fromisoformat(resource_start_date).replace(
                        tzinfo=UTC
                    )
                    end_dt = datetime.fromisoformat(resource_end_date).replace(
                        tzinfo=UTC
                    )
                    current_dt = (
                        datetime.now(start_dt.tzinfo)
                        if start_dt.tzinfo
                        else datetime.now(UTC)
                    )

                    if current_dt < start_dt:
                        raise QAuthException(
                            reason=f"Project '{self._lexis_project}' has not started yet. Start date: {resource_start_date}",
                            user_id=self._username,
                            resource=self._lexis_project,
                        )
                    elif current_dt > end_dt:
                        raise QAuthException(
                            reason=f"Project '{self._lexis_project}' has expired. End date: {resource_end_date}",
                            user_id=self._username,
                            resource=self._lexis_project,
                        )
                    else:
                        log.debug(
                            "Project is active from %s to %s",
                            resource_start_date,
                            resource_end_date,
                        )

                except ValueError as date_error:
                    log.warning("Could not parse project dates: %s", date_error)

                # Extract HEAppE URL, SW_STACK, QUANTUM_TECHNOLOGY from specifications
                heappe_url = None
                sw_stack = None
                quantum_technology = None
                qaas_version = None

                for spec in assignment_info.get("Specifications", []):
                    if spec.get("Key") == "HEAPPE_URL":
                        heappe_url = spec.get("Value")

                    # stop searching when all keys was found
                    if heappe_url:
                        break

                # Check whether all required keys was found
                if not heappe_url:
                    raise QAuthException(
                        reason="HEAPPE_URL not found in resource specifications",
                        user_id=self._username,
                        resource=self._lexis_project,
                    )

                # Fetch sw stack and quantum technology and qaas_version

                qaas_base = f"{heappe_url}/qaas"
                qaas_info_resp = requests.get(qaas_base, timeout=30)
                qaas_info_resp.raise_for_status()
                qaas_info = qaas_info_resp.json()

                # Pull required fields from returned JSON
                qaas_version = qaas_info.get("qaas_version")
                sw_stack = qaas_info.get("sw_stack")
                quantum_technology = qaas_info.get("quantum_technology")
                if not qaas_version:
                    raise QAuthException(
                        reason="'qaas_version' not found in machine specification at '"
                        + heappe_url
                        + "/qaas'",
                        user_id=self._username,
                        resource=self._lexis_project,
                    )
                if not sw_stack:
                    raise QAuthException(
                        reason="'sw_stack' not found in machine specification at '"
                        + heappe_url
                        + "/qaas'",
                        user_id=self._username,
                        resource=self._lexis_project,
                    )

                # quantum technology is optional

                log.debug(
                    "Found sw_stack: %s, quantum_technology: %s, HEAppE URL: %s, qaas version %s, for resource: %s",
                    sw_stack,
                    "-" if not quantum_technology else quantum_technology,
                    heappe_url,
                    qaas_version,
                    lexis_resource_name,
                )

                ####################
                # Backend Metadata #
                ####################

                backend_info = QBackendMetadata(
                    backend_name=assignment_info["LocationName"],
                    swstack=sw_stack,
                    qaas_version=qaas_version,
                    available="UNKNOWN",  # FIXME: get this information
                    quantum_technology=quantum_technology,
                    lexis_resource=LexisResource(
                        project_resource_info["Name"],
                        assignment_info["AllocationAmount"],
                        assignment_info["ProjectResourceId"],
                        project_resource_info["StartDate"],
                        project_resource_info["EndDate"],
                        heappe_url,
                    ),
                    lexis_project=LexisProject(self._lexis_project),
                    host_entity=assignment_info["LocationName"],
                )

                return backend_info

        except QAuthException:
            raise
        except Exception as e:
            raise QAuthException(
                reason=f"Failed to authorize LEXIS resource: {e!s}",
                user_id=self._username,
                resource=self._lexis_project,
            ) from e

    def _authenticate_heappe(self) -> HEAppEApi:
        """
        Create the HEAppE API client for the resolved backend.

        The client targets the backend's ``HEAPPE_URL`` and sends the LEXIS JWT
        as a ``Bearer`` token; no separate HEAppE session is opened.

        :returns: Configured HEAppE API client.
        :rtype: HEAppEApi

        :raises QAuthException: Client construction failed.
        """

        try:
            # Initialize HEAppE client
            conf: HEAppEConfiguration = HEAppEConfiguration()
            conf.host = self._backend_metadata.lexis_resource.heappe_url

            heappe_client: HEAppEApi = HEAppEApi(
                conf, header_name="Authorization", header_value=f"Bearer {self._token}"
            )
            return heappe_client

        except Exception as e:
            raise QAuthException(
                f"HEAppE authentication failed: {e!s}",
                self._username,
                f"{self._lexis_project}",
            ) from e

    def _python_object_upload_to_cluster(
        self,
        python_object,
        target_file_name: str,
        job_info: SubmittedJobInfoExt,
        use_dill=False,
    ):
        """
        Serialize a Python object and upload it to the job's execution directory.

        :param python_object: Object to serialize.
        :type python_object: Any
        :param target_file_name: File name without extension; ``.pkl`` is appended.
        :type target_file_name: str
        :param job_info: Created (not necessarily submitted) HEAppE job; the file
            goes to the directory of its first task.
        :type job_info: SubmittedJobInfoExt
        :param use_dill: Serialize with :mod:`dill` instead of :mod:`pickle`
            (supports lambdas, closures, etc.).
        :type use_dill: bool

        :raises QException: Upload request failed or HEAppE did not confirm the file.
        """
        files = {
            "files": (
                target_file_name + ".pkl",
                pickle.dumps(python_object, protocol=pickle.HIGHEST_PROTOCOL)
                if not use_dill
                else dill.dumps(python_object, protocol=dill.HIGHEST_PROTOCOL),
                "application/octet-stream",
            )
        }

        full_upload_file_url = (
            self._backend_metadata.lexis_resource.heappe_url
            + QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        )
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=120,
            params={"JobId": job_info.id, "TaskId": job_info.tasks[0].id},
        )

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        # pylint: disable=W0105
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
            if f["Succeeded"] and target_file_name + ".pkl" == f["FileName"]:
                return

        raise QException(
            f"Job meta files failed to be uploaded. Assigned file name is '{target_file_name}'"
        )

    def _circuit_upload_to_cluster(
        self, circuit: str, target_file_name: str, job_info: SubmittedJobInfoExt
    ):
        """
        Upload an OpenQASM circuit to the job's execution directory.

        :param circuit: Circuit serialized as OpenQASM.
        :type circuit: str
        :param target_file_name: File name without extension; ``.qasm`` is appended.
        :type target_file_name: str
        :param job_info: Created HEAppE job; the file goes to the directory of its
            first task.
        :type job_info: SubmittedJobInfoExt

        :raises QException: Upload request failed or HEAppE did not confirm the file.
        """

        file_name = target_file_name + ".qasm"
        files = {"files": (file_name, circuit, "text/plain")}

        full_upload_file_url = (
            self._backend_metadata.lexis_resource.heappe_url
            + QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        )
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=120,
            params={"JobId": job_info.id, "TaskId": job_info.tasks[0].id},
        )

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        # pylint: disable=W0105
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
            if f["Succeeded"] and file_name == f["FileName"]:
                return

        raise QException(
            f"Job meta files failed to be uploaded. Assigned file name is '{file_name}'"
        )

    def _token_upload_to_cluster(
        self, encryption_password: str, job_info: SubmittedJobInfoExt
    ):
        """
        Encrypt the user's JWT and upload it as ``user_token.enc``.

        The job decrypts the token with the password delivered through the
        ``USER_JWT_PWD`` environment variable, so the token never travels in
        plain text inside the job specification.

        :param encryption_password: Raw (not encoded) password used for encryption.
        :type encryption_password: str
        :param job_info: Created HEAppE job; the file goes to the directory of its
            first task.
        :type job_info: SubmittedJobInfoExt

        :raises QException: Upload request failed or HEAppE did not confirm the file.
        """
        encrypted_token = encrypt_string(self._token, encryption_password)

        file_name = "user_token.enc"
        files = {"files": (file_name, encrypted_token, "text/plain")}

        full_upload_file_url = (
            self._backend_metadata.lexis_resource.heappe_url
            + QClient.UPLOAD_FILE_TO_EXECUTION_DIR_ENDPOINT
        )
        upload_resp = requests.post(
            url=full_upload_file_url,
            files=files,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=120,
            params={"JobId": job_info.id, "TaskId": job_info.tasks[0].id},
        )

        if upload_resp.status_code != 200:
            raise QException("Failed to upload quantum job backend!!!")

        # pylint: disable=W0105
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
            if f["Succeeded"] and file_name == f["FileName"]:
                return

        raise QException(
            f"Job meta files failed to be uploaded. Assigned file name is '{file_name}'"
        )

    @classmethod
    def _get_real_template_name(cls, project_name: str, template_name: str) -> str:
        """
        Build the project-scoped HEAppE command template name.

        :param project_name: LEXIS project short name.
        :type project_name: str
        :param template_name: Base template name, e.g. ``RunQInit``.
        :type template_name: str
        :returns: ``"<project_name>_<template_name>"``.
        :rtype: str
        """
        return project_name + "_" + template_name

    @property
    def heappe_client(self) -> HEAppEApi | None:
        """
        HEAppE API client.

        :returns: The client, or ``None`` after :meth:`close_session`.
        :rtype: HEAppEApi or None
        """
        return self._heappe_client

    @property
    def is_authenticated(self) -> bool:
        """
        Whether the client is authenticated and holds a token.

        :rtype: bool
        """
        return self._authenticated and self._token is not None

    @property
    def lexis_project(self) -> str:
        """
        LEXIS project short name.

        :rtype: str
        """
        return self._lexis_project

    def get_quantum_backend_info(self) -> QBackendMetadata:
        """
        Return metadata of the backend resolved during initialization.

        No network call is made; the metadata was collected by
        :meth:`_authorize_lexis_resource`.

        :returns: Backend metadata (name, software stack, technology, LEXIS
            resource and project, HEAppE URL, ``qaas`` version).
        :rtype: QBackendMetadata

        :raises QAuthException: Client is not authenticated.

        .. note::
            Backend availability is currently reported as ``"UNKNOWN"``.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        return self._backend_metadata

    def submit_quantum_job(
        self,
        job_data: dict[str, Any],
        backend: IQMBackend | Pulla = None,
        circuits: RunDefinition | str | list[str] | None = None,
        run_options: dict[str, Any] | None = None,
    ) -> int:
        """
        Create, populate and submit a HEAppE job.

        Workflow:

        #. Pick the queue: ``qexecute`` (2 cores) if ``backend`` is given,
           otherwise ``qinit`` (1 core).
        #. Create the HEAppE job.
        #. Upload the encrypted user token.
        #. Upload inputs in parallel:

           * ``RunDefinition`` -> ``run_definition.pkl`` (dill);
           * OpenQASM string(s) -> ``circuit_NNNN.qasm``;
           * ``run_options`` -> ``run_kwargs.pkl`` (dill);
           * :class:`IQMBackend` -> ``backend.pkl`` and ``iqm_target_attrs.pkl``
             (pickle; the latter restores ``iqm_*`` attributes of the target that
             are lost when pickling);
           * :class:`Pulla` -> ``pulla.pkl`` (dill).

        #. Submit the job to the queue.

        The remote command is selected via ``Q_COMMAND``:
        ``pulla_submit_playlist`` for a ``RunDefinition``, ``backend_run``
        otherwise.

        :param job_data: Optional overrides of the job specification:

            * ``name`` (*str*) -- job name, default ``"quantum_job"``;
            * ``walltime_limit`` (*int*) -- seconds, default ``7200``;
            * ``project_id``, ``cluster_id``, ``file_transfer_method_id``
              (*int*) -- HEAppE IDs, default from the resolved template;
            * ``tasks`` (*list[dict]*) -- only the first item is used; keys
              ``cluster_node_type_id`` and ``template_parameter_values``;
            * ``command_template_id`` (*list[dict]*) -- only the first item is
              used; key ``command_template_id``;
            * ``environment_variables`` (*list[EnvironmentVariableExt]*) --
              prepended to the variables set by the client.

        :type job_data: dict[str, Any]
        :param backend: Backend object executed remotely. Determines the queue.
        :type backend: IQMBackend or Pulla or None
        :param circuits: Pulla ``RunDefinition`` or one or more OpenQASM circuits.
        :type circuits: RunDefinition or str or list[str] or None
        :param run_options: Keyword arguments for the remote run call.
        :type run_options: dict[str, Any] or None

        :returns: HEAppE job ID.
        :rtype: int

        :raises QAuthException: Client is not authenticated or HEAppE returned 401.
        :raises QException: Job creation, upload or submission failed.

        Example::

            job_id = client.submit_quantum_job(
                {"name": "bell", "walltime_limit": 1800},
                backend=iqm_backend,
                circuits=qasm_str,
                run_options={"shots": 1000},
            )
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle user token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        queue = "qexecute" if backend else "qinit"
        heappe_project_id = self._command_template_infos[queue]["target_project_id"]
        heappe_cluster_id = self._command_template_infos[queue]["target_location_id"]
        heappe_node_type_id = self._command_template_infos[queue]["target_node_type_id"]
        heappe_command_template_id = self._command_template_infos[queue][
            "target_template_id"
        ]
        heappe_file_transfer_method_id = self._command_template_infos[queue][
            "target_node_type_file_transfer_method_id"
        ]
        minmax_cores = 1 if queue == "qinit" else 2

        log.debug(
            "LEXIS_PROJECT_RESOURCE_ID: %s",
            str(self._backend_metadata.lexis_resource.project_resource_id),
        )

        try:
            env_variables = [
                *job_data.get("environment_variables", []),
                # For PULLA
                EnvironmentVariableExt(
                    "Q_COMMAND",
                    "pulla_submit_playlist"
                    if isinstance(circuits, RunDefinition)
                    else "backend_run",
                ),
                # EnvironmentVariableExt("Q_COMMAND", "backend_run"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt(
                    "LEXIS_PROJECT_RESOURCE_ID",
                    str(self._backend_metadata.lexis_resource.project_resource_id),
                ),
            ]
            if self._socket_path:
                env_variables.append(
                    EnvironmentVariableExt("IQM_SERVICE_SOCKET", self._socket_path)
                )
            # Create job specification
            job_spec = JobSpecification(
                name=job_data.get("name", "quantum_job"),
                project_id=job_data.get("project_id", heappe_project_id),
                cluster_id=job_data.get("cluster_id", heappe_cluster_id),
                tasks=[
                    TaskSpecification(
                        name="quantum_task",
                        min_cores=minmax_cores,
                        max_cores=minmax_cores,
                        walltime_limit=job_data.get("walltime_limit", 7200),
                        progress_file="quantum_task_progress.log",
                        log_file="quantum_task_progress.log",
                        cluster_node_type_id=job_data.get(
                            "tasks", [{"cluster_node_type_id": heappe_node_type_id}]
                        )[0].get("cluster_node_type_id", heappe_node_type_id),
                        command_template_id=job_data.get(
                            "command_template_id",
                            [{"command_template_id": heappe_command_template_id}],
                        )[0].get("command_template_id", heappe_command_template_id),
                        template_parameter_values=job_data.get(
                            "tasks", [{"template_parameter_values": []}]
                        )[0].get("template_parameter_values", []),
                    )
                ],
                file_transfer_method_id=job_data.get(
                    "file_transfer_method_id", heappe_file_transfer_method_id
                ),
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = (
                    heappe_job_management_api.heappe_job_management_create_job_post(
                        body=job_spec_model
                    )
                )
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to create job: {e.reason}; API status: {e.status}"
                ) from e

            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                upload_futures = []
                ####################
                # Upload circuits #
                ####################
                # if circuits is set, upload it to execution directory of job
                if (
                    circuits
                    and isinstance(circuits, RunDefinition)
                    or (
                        isinstance(circuits, list)
                        and isinstance(circuits[0], RunDefinition)
                    )
                ):
                    upload_futures.append(
                        executor.submit(
                            self._python_object_upload_to_cluster,
                            circuits,
                            "run_definition",
                            job_info,
                            True,
                        )
                    )
                elif circuits:
                    q_circuits = circuits if isinstance(circuits, list) else [circuits]
                    for index, circuit in enumerate(q_circuits):
                        upload_futures.append(
                            executor.submit(
                                self._circuit_upload_to_cluster,
                                circuit,
                                f"circuit_{index:04d}",
                                job_info,
                            )
                        )

                #####################
                # Upload run_kwargs #
                #####################
                # if run_kwargs is set, upload it to execution directory of job
                if run_options:
                    upload_futures.append(
                        executor.submit(
                            self._python_object_upload_to_cluster,
                            run_options,
                            "run_kwargs",
                            job_info,
                            True,
                        )
                    )

                ##################
                # Upload backend #
                ##################
                if backend and (isinstance(backend, IQMBackend)):
                    upload_futures.append(
                        executor.submit(
                            self._python_object_upload_to_cluster,
                            backend,
                            "backend",
                            job_info,
                            False,
                        )
                    )
                    # Fixes loading of iqm_ attrs of IQMTarget
                    if isinstance(backend, IQMBackend):

                        def _get_iqm_target_attrs(backend):
                            # Covering fix, that iqm_ attributes are not mentioned in class definition of IQMTarget, so pickling does not save them
                            return {
                                attr: getattr(backend.target, attr)
                                for attr in dir(backend.target)
                                if attr.startswith("iqm_") and not attr.startswith("__")
                            }

                        iqm_target_attrs = _get_iqm_target_attrs(backend)
                        upload_futures.append(
                            executor.submit(
                                self._python_object_upload_to_cluster,
                                iqm_target_attrs,
                                "iqm_target_attrs",
                                job_info,
                                False,
                            )
                        )
                elif backend and isinstance(backend, Pulla):
                    upload_futures.append(
                        executor.submit(
                            self._python_object_upload_to_cluster,
                            backend,
                            "pulla",
                            job_info,
                            True,
                        )
                    )

                done, pending = concurrent.futures.wait(
                    upload_futures, return_when=concurrent.futures.FIRST_EXCEPTION
                )
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
                    body=submit_model
                )
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to submit job: {e.reason}; API status: {e.status}"
                ) from e
            return job_info.id

        except QException:
            raise
        except Exception as e:
            import traceback

            traceback.print_exc(file=sys.stderr)
            raise QException("Job submission failed!!!") from e

    def get_job_status(self, job_id: int) -> tuple[str, int, list[int]]:
        """
        Query the status of a HEAppE job.

        HEAppE job states are mapped as follows:

        ========== ==========================================================
        Status     Meaning
        ========== ==========================================================
        WAITING    Queued, running, or waiting for a service account
        FINISHED   Completed successfully
        FAILED     Failed
        CANCELED   Canceled (terminal, but not a failure)
        UNKNOWN    HEAppE response has no state
        ========== ==========================================================

        :param job_id: HEAppE job ID.
        :type job_id: int

        :returns: ``(status, job_id, task_ids)``; for ``UNKNOWN`` the result is
            ``("UNKNOWN", 0, [])``.
        :rtype: tuple[str, int, list[int]]

        :raises QAuthException: Client is not authenticated.
        :raises QException: Status query failed (HEAppE errors, including 401,
            are wrapped).

        Example::

            status, job_id, task_ids = client.get_job_status(12345)
            if status in QClient.TERMINAL_JOB_STATES:
                ...
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
                raise QException(
                    f"Unable to get job info: {e.reason}; API status: {e.status}"
                ) from e

            if hasattr(job_info, "state"):
                job_state = JobState(job_info.state)
                log.debug(
                    "Job '%d' state '%d', failed_code '%d', %s",
                    job_id,
                    job_info.state,
                    JobState.Failed.value,
                    str(job_state == JobState.Failed),
                )
                is_finished: bool = (
                    job_state.value > JobState.Running.value
                    and job_state != JobState.WaitingForServiceAccount
                )

                # Reported apart from FAILED: a canceled job is terminal but did not fail,
                # and maps to JobStatus.CANCELLED rather than JobStatus.ERROR.
                if job_state == JobState.Canceled:
                    return "CANCELED", job_info.id, [task.id for task in job_info.tasks]
                if job_state == JobState.Failed:
                    return "FAILED", job_info.id, [task.id for task in job_info.tasks]
                if is_finished:
                    return "FINISHED", job_info.id, [task.id for task in job_info.tasks]
                return "WAITING", job_info.id, [task.id for task in job_info.tasks]
            else:
                return "UNKNOWN", 0, []

        except Exception as e:
            raise QException("Failed to get job status!") from e

    def get_job_results(
        self,
        job_id: int,
        file_names_to_fetch: list[str] | None = None,
        use_dill: list[bool] | None = None,
        job_status=None,
        task_ids=None,
        wait=False,
    ) -> dict[str, Any]:
        """
        Download and deserialize result files of a HEAppE job.

        Behaviour by job status:

        * ``FINISHED`` -- each file in ``file_names_to_fetch`` is downloaded and
          unpickled; returns a dict keyed by the file's base name without
          extension, plus ``"status": "FINISHED"``.
        * ``FAILED`` -- ``stderr.txt`` and ``stdout.txt`` of the first task are
          downloaded and raised inside :class:`QResultsFailed`.
        * ``CANCELED`` -- raises :class:`QResultsFailed`.
        * otherwise -- returns ``None`` (unless ``wait`` is set).

        :param job_id: HEAppE job ID.
        :type job_id: int
        :param file_names_to_fetch: Paths relative to the HEAppE job context,
            typically ``/<job_id>/<task_id>/<name>.pkl``. Nothing is fetched when
            ``None``.
        :type file_names_to_fetch: list[str] or None
        :param use_dill: Per-file flag, same length as ``file_names_to_fetch``;
            ``True`` deserializes with :mod:`dill`, ``False`` with :mod:`pickle`.
        :type use_dill: list[bool] or None
        :param job_status: Already known status; skips the initial status query
            when given together with ``task_ids``.
        :type job_status: str or None
        :param task_ids: Already known task IDs of the job.
        :type task_ids: list[int] or None
        :param wait: Poll every :attr:`DEFAULT_POLL_TIME` seconds until the job
            reaches a state in :attr:`TERMINAL_JOB_STATES`.
        :type wait: bool

        :returns: Deserialized results, or ``None`` if the job has not finished.
        :rtype: dict[str, Any] or None

        :raises QAuthException: Client is not authenticated.
        :raises QResultsFailed: Job failed or was canceled, or retrieval failed.
        :raises QException: A result file could not be downloaded or deserialized.

        .. warning::
            Files are deserialized with :mod:`pickle` / :mod:`dill`; only fetch
            results from trusted backends.

        Example::

            res = client.get_job_results(
                job_id,
                [f"/{job_id}/{task_id}/results.pkl"],
                use_dill=[False],
                wait=True,
            )
            counts = res["results"].get_counts()
        """

        if not self.is_authenticated:
            raise QAuthException(
                reason="Client not authenticated", resource=f"job_{job_id}"
            )

        if file_names_to_fetch is None:
            file_names_to_fetch = []

        try:
            if not job_status or not task_ids:
                job_status, job_id, task_ids = self.get_job_status(job_id)
            log.debug("get_job_results status: %s", job_status)
            if wait:
                while job_status not in QClient.TERMINAL_JOB_STATES:
                    time.sleep(QClient.DEFAULT_POLL_TIME)
                    job_status, _, _ = self.get_job_status(job_id)
            # Canceled, terminal: no results will ever be produced, and there is no
            # failure reason to look for in stderr.
            if job_status == "CANCELED":
                raise QResultsFailed(job_id, "Job was canceled.")

            # Failed, try to find a reason
            if job_status == "FAILED":
                stderr_content = "None"
                stdout_content = "None"
                try:
                    file_content_stderr = self._download_file_from_cluster_binary(
                        submitted_job_context_id=job_id,
                        relative_file_path=f"/{job_id}/{task_ids[0]}/stderr.txt",
                    )

                    file_content_stdout = self._download_file_from_cluster_binary(
                        submitted_job_context_id=job_id,
                        relative_file_path=f"/{job_id}/{task_ids[0]}/stdout.txt",
                    )

                    # Unpickle the results
                    stdout_content = file_content_stdout.decode("utf-8")
                    stderr_content = file_content_stderr.decode("utf-8")
                except Exception as e:
                    raise QResultsFailed(job_id) from e
                raise QResultsFailed(
                    job_id,
                    "Failure reason (stderr):\n"
                    + stderr_content
                    + "\n-------\n (stdout):\n"
                    + stdout_content,
                )

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
                        submitted_job_context_id=job_id, relative_file_path=file_name
                    )

                    # Unpickle the results
                    unpickled_data = (
                        dill.loads(file_content)
                        if use_dill[idx]
                        else pickle.loads(file_content)
                    )
                    results_data[file_name.split("/")[-1].split(".")[0]] = (
                        unpickled_data
                    )

                except Exception as file_error:
                    log.warning(
                        "Could not download or unpickle %s: %s", file_name, file_error
                    )
                    raise QException(
                        f"Could not download or unpickle {file_name}: "
                    ) from file_error

            return {**results_data, "status": "FINISHED"}

        except QException:
            raise
        except Exception as e:
            raise QResultsFailed(f"Failed to get job ({job_id}) results: {e!s}") from e

    def get_pulla(self) -> tuple[dict[str, Any], Pulla]:
        """
        Initialize IQM Pulla remotely.

        Runs a ``pulla_init`` job on the ``qinit`` queue (60 s walltime), waits
        for it and returns the data needed to construct a local ``QPulla``
        without the client calling the IQM API directly.

        :returns: ``(pulla_data, pulla)`` -- initialization data and the
            :class:`Pulla` instance created on the remote side.
        :rtype: tuple[dict[str, Any], Pulla]

        :raises QAuthException: Client is not authenticated or HEAppE returned 401.
        :raises QResultsFailed: The init job failed or was canceled.
        :raises QException: Any other failure.
        """

        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        QUEUE = "qinit"
        heappe_project_id = self._command_template_infos[QUEUE]["target_project_id"]
        heappe_cluster_id = self._command_template_infos[QUEUE]["target_location_id"]
        heappe_node_type_id = self._command_template_infos[QUEUE]["target_node_type_id"]
        heappe_command_template_id = self._command_template_infos[QUEUE][
            "target_template_id"
        ]
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE][
            "target_node_type_file_transfer_method_id"
        ]
        MINMAX_CORES = 1
        try:
            env_variables = [
                EnvironmentVariableExt("Q_COMMAND", "pulla_init"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt(
                    "LEXIS_PROJECT_RESOURCE_ID",
                    str(self._backend_metadata.lexis_resource.project_resource_id),
                ),
            ]
            # Create job specification
            job_spec = JobSpecification(
                name="quantum_pulla_init",
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[
                    TaskSpecification(
                        name="quantum_task",
                        min_cores=MINMAX_CORES,
                        max_cores=MINMAX_CORES,
                        walltime_limit=60,
                        progress_file="quantum_task_progress.log",
                        log_file="quantum_task_progress.log",
                        cluster_node_type_id=heappe_node_type_id,
                        command_template_id=heappe_command_template_id,
                        template_parameter_values=[],
                    )
                ],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = (
                    heappe_job_management_api.heappe_job_management_create_job_post(
                        body=job_spec_model
                    )
                )
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to create job: {e.reason}; API status: {e.status}"
                ) from e

            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)

            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model
                )
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to submit job: {e.reason}; API status: {e.status}"
                ) from e

            # Fetch results of initialization
            results = self.get_job_results(
                job_info.id,
                [
                    f"/{job_info.id}/{job_info.tasks[0].id}/pulla_data.pkl",
                    f"/{job_info.id}/{job_info.tasks[0].id}/pulla.pkl",
                ],
                use_dill=[True, True],
                task_ids=[job_info.tasks[0].id],
                wait=True,
            )
            return results["pulla_data"], results["pulla"]

        except QException:
            raise
        except Exception as e:
            raise QException("Failed to initialize Pulla!!!") from e

    def get_calibration_set(self, calibration_set_id: UUID | None) -> CalibrationSet:
        """
        Fetch a calibration set from the quantum computer.

        Runs a ``get_calibration_set`` job on the ``qinit`` queue (60 s walltime)
        and waits for its result.

        :param calibration_set_id: Calibration set ID; the default (current)
            calibration set is fetched when ``None``.
        :type calibration_set_id: UUID or None

        :returns: The calibration set.
        :rtype: CalibrationSet

        :raises QAuthException: Client is not authenticated or HEAppE returned 401.
        :raises QResultsFailed: The job failed or was canceled.
        :raises QException: Any other failure.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        QUEUE = "qinit"
        heappe_project_id = self._command_template_infos[QUEUE]["target_project_id"]
        heappe_cluster_id = self._command_template_infos[QUEUE]["target_location_id"]
        heappe_node_type_id = self._command_template_infos[QUEUE]["target_node_type_id"]
        heappe_command_template_id = self._command_template_infos[QUEUE][
            "target_template_id"
        ]
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE][
            "target_node_type_file_transfer_method_id"
        ]
        MINMAX_CORES = 1
        try:
            env_variables = [
                EnvironmentVariableExt("Q_COMMAND", "get_calibration_set"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt(
                    "LEXIS_PROJECT_RESOURCE_ID",
                    str(self._backend_metadata.lexis_resource.project_resource_id),
                ),
                EnvironmentVariableExt("Q_OPTIONAL_ARG", str(calibration_set_id)),
            ]
            # Create job specification
            job_spec = JobSpecification(
                name="quantum_get_calibration_set",
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[
                    TaskSpecification(
                        name="quantum_task",
                        min_cores=MINMAX_CORES,
                        max_cores=MINMAX_CORES,
                        walltime_limit=60,
                        progress_file="quantum_task_progress.log",
                        log_file="quantum_task_progress.log",
                        cluster_node_type_id=heappe_node_type_id,
                        command_template_id=heappe_command_template_id,
                        template_parameter_values=[],
                    )
                ],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = (
                    heappe_job_management_api.heappe_job_management_create_job_post(
                        body=job_spec_model
                    )
                )
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to create job: {e.reason}; API status: {e.status}"
                ) from e

            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)

            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model
                )
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to submit job: {e.reason}; API status: {e.status}"
                ) from e

            # Fetch results of initialization
            results = self.get_job_results(
                job_info.id,
                [f"/{job_info.id}/{job_info.tasks[0].id}/calibration_set.pkl"],
                use_dill=[True],
                task_ids=[job_info.tasks[0].id],
                wait=True,
            )
            return results["calibration_set"]

        except QException:
            raise
        except Exception as e:
            raise QException("Failed to fetch calibration set!!!") from e

    def get_dynamic_architecture(
        self, calibration_set_id: UUID | None = None
    ) -> DynamicQuantumArchitecture:
        """
        Fetch the dynamic quantum architecture for a calibration set.

        The dynamic architecture describes the qubits, couplers and gates
        (with their implementations) that are calibrated, i.e. currently usable,
        in the given calibration set. Results are cached per calibration set ID.

        Runs a ``get_dynamic_quantum_architecture`` job on the ``qinit`` queue
        (60 s walltime) on a cache miss.

        :param calibration_set_id: Calibration set ID; the default calibration set
            is used when ``None``.
        :type calibration_set_id: UUID or None

        :returns: Dynamic quantum architecture.
        :rtype: DynamicQuantumArchitecture

        :raises QAuthException: Client is not authenticated or HEAppE returned 401.
        :raises QResultsFailed: The job failed or was canceled.
        :raises QException: Any other failure.

        .. note::
            Results are cached under the returned architecture's actual
            ``calibration_set_id``, so calls with ``None`` are not served from
            the cache.
        """
        _calibration_set_id = (
            calibration_set_id if calibration_set_id is not None else "default"
        )

        if _calibration_set_id in self._dynamic_quantum_architectures:
            return self._dynamic_quantum_architectures[_calibration_set_id]

        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        # handle token
        raw_encrypt_pwd, encoded_pwd = generate_password(50)

        # Determine whether to select qinit or qexecute
        QUEUE = "qinit"
        heappe_project_id = self._command_template_infos[QUEUE]["target_project_id"]
        heappe_cluster_id = self._command_template_infos[QUEUE]["target_location_id"]
        heappe_node_type_id = self._command_template_infos[QUEUE]["target_node_type_id"]
        heappe_command_template_id = self._command_template_infos[QUEUE][
            "target_template_id"
        ]
        heappe_file_transfer_method_id = self._command_template_infos[QUEUE][
            "target_node_type_file_transfer_method_id"
        ]
        MINMAX_CORES = 1
        try:
            env_variables = [
                EnvironmentVariableExt("Q_COMMAND", "get_dynamic_quantum_architecture"),
                EnvironmentVariableExt("USER_JWT_PWD", encoded_pwd),
                EnvironmentVariableExt("LEXIS_PROJECT", self.lexis_project),
                EnvironmentVariableExt(
                    "LEXIS_PROJECT_RESOURCE_ID",
                    str(self._backend_metadata.lexis_resource.project_resource_id),
                ),
                EnvironmentVariableExt("Q_OPTIONAL_ARG", str(_calibration_set_id)),
            ]
            # Create job specification
            job_spec = JobSpecification(
                name="quantum_get_dynamic_architecture",
                project_id=heappe_project_id,
                cluster_id=heappe_cluster_id,
                tasks=[
                    TaskSpecification(
                        name="quantum_task",
                        min_cores=MINMAX_CORES,
                        max_cores=MINMAX_CORES,
                        walltime_limit=60,
                        progress_file="quantum_task_progress.log",
                        log_file="quantum_task_progress.log",
                        cluster_node_type_id=heappe_node_type_id,
                        command_template_id=heappe_command_template_id,
                        template_parameter_values=[],
                    )
                ],
                file_transfer_method_id=heappe_file_transfer_method_id,
                environment_variables=env_variables,
            )

            job_spec_model = CreateJobByProjectModel(job_specification=job_spec)

            heappe_job_management_api = JobManagementApi(self._heappe_client)

            # Submit job
            try:
                job_info: SubmittedJobInfoExt = (
                    heappe_job_management_api.heappe_job_management_create_job_post(
                        body=job_spec_model
                    )
                )
                if not job_info:
                    raise QException("Job creation failed!!!")
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to create job: {e.reason}; API status: {e.status}"
                ) from e

            # upload token
            self._token_upload_to_cluster(raw_encrypt_pwd, job_info)

            ########################
            # Submit job to HEAppE #
            ########################
            submit_model = SubmitJobModel(created_job_info_id=job_info.id)
            # Submit to queue
            try:
                heappe_job_management_api.heappe_job_management_submit_job_put(
                    body=submit_model
                )
            except ApiException as e:
                if e.status == 401:
                    raise QAuthException("Unauthorized!!!") from e
                raise QException(
                    f"Unable to submit job: {e.reason}; API status: {e.status}"
                ) from e

            # Fetch results of initialization
            results = self.get_job_results(
                job_info.id,
                [
                    f"/{job_info.id}/{job_info.tasks[0].id}/dynamic_quantum_architecture.pkl"
                ],
                use_dill=[True],
                task_ids=[job_info.tasks[0].id],
                wait=True,
            )
            dynamic_quantum_architecture = results["dynamic_quantum_architecture"]
            self._dynamic_quantum_architectures[
                dynamic_quantum_architecture.calibration_set_id
            ] = dynamic_quantum_architecture

            return dynamic_quantum_architecture

        except QException:
            raise
        except Exception as e:
            raise QException("Failed to fetch calibration set!!!") from e

    def get_available_backends(self) -> dict[str, Any]:
        """
        List quantum backends available to the LEXIS project.

        Reads the project's resources from the UserOrg API and returns every
        assignment of a resource with ``LocationTypeId == 7`` (quantum) whose
        ``AggregationName`` is an accepted aggregation name. ``HEAPPE_URL``,
        ``SW_STACK`` and ``QUANTUM_TECHNOLOGY`` are taken from the assignment
        specifications (``"UNKNOWN"`` when missing).

        :returns: Mapping of backend (location) name to its metadata.
        :rtype: dict[str, QBackendMetadata]

        :raises QAuthException: Client is not authenticated or the resources
            could not be retrieved.
        """
        if not self._authenticated:
            raise QAuthException("Unauthorized!!!")

        try:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }

            # Get project resources
            project_resources_url = f"{self._lexis_userorg_api_url}/api/ProjectResource"
            response = requests.get(
                project_resources_url,
                headers=headers,
                params={"ProjectShortName": self._lexis_project},
                timeout=30,
            )
            response.raise_for_status()

            project_resources = response.json()

            log.debug("Project resource: %s", str(project_resources))

            # Find the project by name or ID
            backend_metadata_list: list[QBackendMetadata] = []
            for project_resource_info in project_resources:
                for assignment_info in project_resource_info.get("Assignments", []):
                    location_type_id = project_resource_info.get("LocationTypeId")
                    if (
                        location_type_id == 7
                    ):  # LocationTypeId 7 corresponds to locations in LEXIS, which we use for quantum backends
                        aggregation_name = project_resource_info.get("AggregationName")
                        if aggregation_name in QClient.DEFAULT_LEXIS_AGGREGATION_NAME:
                            quantum_technology = "UNKNOWN"
                            sw_stack = "UNKNOWN"
                            heappe_url = None
                            for spec in assignment_info.get("Specifications", []):
                                if spec.get("Key") == "HEAPPE_URL":
                                    heappe_url = spec.get("Value")
                                elif spec.get("Key") == "SW_STACK":
                                    sw_stack = spec.get("Value")
                                elif spec.get("Key") == "QUANTUM_TECHNOLOGY":
                                    quantum_technology = spec.get("Value")

                            qmetadata = QBackendMetadata(
                                backend_name=assignment_info["LocationName"],
                                swstack=sw_stack,
                                available="UNKNOWN",  # FIXME: get this information
                                quantum_technology=quantum_technology,
                                lexis_resource=LexisResource(
                                    project_resource_info["Name"],
                                    assignment_info["AllocationAmount"],
                                    assignment_info["ProjectResourceId"],
                                    project_resource_info["StartDate"],
                                    project_resource_info["EndDate"],
                                    heappe_url,
                                ),
                                lexis_project=LexisProject(self._lexis_project),
                                host_entity=assignment_info["LocationName"],
                            )
                            backend_metadata_list.append(qmetadata)
            return {
                metadata.backend_name: metadata for metadata in backend_metadata_list
            }
        except Exception:  # noqa: BLE001
            raise QAuthException(
                reason=f"Failed to retrieve resources for LEXIS project '{self._lexis_project}', please verify your assignment and try again!",
                user_id=self._username,
            )

    def _download_file_from_cluster_binary(
        self, submitted_job_context_id: int, relative_file_path: str
    ) -> bytes:
        """
        Download a file from a HEAppE job directory as raw bytes.

        HEAppE returns file content as a base64-encoded JSON string; this method
        decodes it.

        :param submitted_job_context_id: HEAppE job ID.
        :type submitted_job_context_id: int
        :param relative_file_path: Path relative to the job context, e.g.
            ``/<job_id>/<task_id>/results.pkl``.
        :type relative_file_path: str

        :returns: File content.
        :rtype: bytes

        :raises QAuthException: HEAppE returned 401.
        :raises QException: Download failed.
        """

        download_body = DownloadFileFromClusterModel(
            submitted_job_info_id=submitted_job_context_id,
            relative_file_path=relative_file_path,
        )

        api_request_body = {"_preload_content": False, "body": download_body}

        file_transfer_api = FileTransferApi(self.heappe_client)
        try:
            response = (
                file_transfer_api.heappe_file_transfer_download_file_from_cluster_post(
                    **api_request_body
                )
            )
        except ApiException as e:
            if e.status == 401:
                raise QAuthException("Unauthorized!!!") from e
            raise QException(
                f"Unable to download file from cluster: {e.reason}; API status: {e.status}"
            ) from e
        return base64.b64decode(json.loads(response.data))

    def _get_command_template_ids(
        self,
        template_name_qinit: str,
        template_name_qexecute: str,
        qinit_queue_name: str,
        qexecute_queue_name: str,
    ) -> dict[str, dict[str, int]]:
        """
        Resolve HEAppE IDs needed to create jobs on both queues.

        For each template, HEAppE cluster information is queried and the
        following are located: the cluster matching the backend name, a node
        type (queue) whose project ``accounting_string`` equals the LEXIS
        resource name, and the command template in that project. The plain
        template name is tried first, then the project-scoped name
        ``<lexis_project>_<template>``.

        :param template_name_qinit: Command template for the ``qinit`` queue.
        :type template_name_qinit: str
        :param template_name_qexecute: Command template for the ``qexecute`` queue.
        :type template_name_qexecute: str
        :param qinit_queue_name: Node type name of the init queue.
        :type qinit_queue_name: str
        :param qexecute_queue_name: Node type name of the execution queue.
        :type qexecute_queue_name: str

        :returns: ``{"qinit": info, "qexecute": info}`` where ``info`` contains
            ``target_location_id``, ``target_node_type_id``,
            ``target_project_id``, ``target_template_id``,
            ``target_node_type_file_transfer_method_id``, ``qinit_queue_name``
            and ``qexecute_queue_name``.
        :rtype: dict[str, dict[str, int | str]]

        :raises QException: Cluster query failed, or location, project or
            template was not found.

        .. note::
            A node type is accepted if its name matches *either* queue name, so
            the queue actually selected depends on the template's availability.
        """

        def get_command_template_call(lexis_project, target_template_name):

            for template_name in [
                target_template_name,
                lexis_project + "_" + target_template_name,
            ]:
                # Fallback to custom command template naming in HEAppE (<lexis_project>_RunQInit etc.)
                try:
                    # This would typically call HEAppE API to list command templates
                    # For now, using a placeholder implementation
                    heappe_cluster_info_api = ClusterInformationApi(self._heappe_client)

                    # Get available command templates (API endpoint may vary)
                    clusters: ClusterExt = heappe_cluster_info_api.heappe_cluster_information_list_available_clusters_get(
                        ClusterName=self._backend_metadata.backend_name,
                        CommandTemplateName=template_name,
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
                    log.debug(
                        "Falling back to old command template naming (target_location not found)"
                    )
                    continue

                # Fetch information about node type (partition), cluster etc.
                log.debug(
                    "Queue names to be searched: %s, %s",
                    qinit_queue_name,
                    qexecute_queue_name,
                )

                for node_type_in_location in target_location.node_types:
                    if node_type_in_location.name in [
                        qinit_queue_name,
                        qexecute_queue_name,
                    ]:
                        for project_in_node_type in node_type_in_location.projects:
                            if (
                                project_in_node_type.accounting_string
                                == self._backend_metadata.lexis_resource.resource_name
                            ):
                                target_node_type = node_type_in_location
                                target_project = project_in_node_type
                                break
                    if target_project:
                        break

                if not target_project:
                    log.debug(
                        "Falling back to old command template naming (target_project not found)"
                    )
                    continue

                log.debug("target_project: %s", str(target_project))
                log.debug(
                    "target_project.command_templates: %s",
                    str(target_project.command_templates),
                )
                for template in target_project.command_templates:
                    if template.name == template_name:
                        target_template = template
                        break

                if target_template:
                    # Everything found, not need to fallback!
                    break
                else:
                    log.debug(
                        "Falling back to old command template naming (target_template not found)"
                    )
                    continue

            #######################################
            # All possibilities have been explored #
            #######################################

            # Verify all required information was found, otherwise fail

            if not target_location:
                raise QException(
                    f"Quantum location '{self._backend_metadata.backend_name}' and resource '{self._backend_metadata.lexis_resource.resource_name}' not found in HEAppE"
                )

            if not target_project:
                log.warning(
                    f"Verify all command templates are accessible to your user for Project '{self._backend_metadata.lexis_resource.resource_name}'!"
                )

                raise QException(
                    f"Project '{self._backend_metadata.lexis_resource.resource_name}' in HEAppE not found"
                )

            if not target_template:
                raise QException(
                    f"Command template '{target_template_name}' not found in HEAppE"
                )
            return {
                "target_location_id": target_location.id,
                "target_node_type_id": target_node_type.id,
                "target_project_id": target_project.id,
                "target_template_id": target_template.id,
                "target_node_type_file_transfer_method_id": target_node_type.file_transfer_method_id,
                "qinit_queue_name": qinit_queue_name,
                "qexecute_queue_name": qexecute_queue_name,
            }

        log.debug(
            "Template names to be searched: %s, %s",
            template_name_qinit,
            template_name_qexecute,
        )

        template_infos = {
            "qinit": get_command_template_call(self.lexis_project, template_name_qinit),
            "qexecute": get_command_template_call(
                self.lexis_project, template_name_qexecute
            ),
        }

        return template_infos

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a queued or running HEAppE job.

        :param job_id: HEAppE job ID (converted to ``int`` internally).
        :type job_id: str or int

        :returns: ``True`` if HEAppE accepted the request, ``False`` on an
            unexpected (non-API) error.
        :rtype: bool

        :raises QAuthException: Client is not authenticated or HEAppE returned 401.
        :raises QException: HEAppE rejected the request.

        .. note::
            Acceptance does not guarantee the job ends as ``CANCELED``; a job
            that is already finishing may still end as ``FINISHED`` or ``FAILED``.
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
            raise QException(
                f"Unable to cancel job: {e.reason}; API status: {e.status}"
            ) from e
        except Exception as e:  # pylint: disable=W0718  # noqa: BLE001
            log.error("Error cancelling job: %s", str(e))
            return False

    def close_session(self):
        """
        Release the HEAppE client and mark the client as unauthenticated.

        No remote call is made (HEAppE is accessed with the stateless JWT).
        Called automatically when the client is used as a context manager.

        :raises QAuthException: Cleanup failed.
        """
        if self._heappe_client:
            try:
                self._authenticated = False
                self._heappe_client = None
            except Exception as e:
                log.error("Error closing session: %s", str(e))
                raise QAuthException("Error closing exception") from e

    def __enter__(self):
        """
        Enter the context manager.

        :returns: This client.
        :rtype: QClient
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Exit the context manager and call :meth:`close_session`.

        Exceptions raised inside the ``with`` block are not suppressed.
        """
        self.close_session()