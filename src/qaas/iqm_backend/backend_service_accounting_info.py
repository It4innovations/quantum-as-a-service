import sys
from datetime import datetime, timezone, timedelta
import aiohttp
import asyncio
import jwt
import requests

from qaas.iqm_backend.backend_env_variables import (
    HEAPPE_REPORTED_AUTH_HEADER,
    QAAS_LEXIS_API_URL,
    QAAS_PROVIDER_NAME,
)


class AccountingInfo:
    """
    Info about submitting user and used resources, needed for accounting record in Cyclops.
    """

    def __init__(
        self,
        user_jwt: bytes,
        submitter_email: str,
        lexis_project: str,
        lexis_project_resource_id: str,
    ):
        self._user_jwt = user_jwt
        self._submitter_email = submitter_email
        self._lexis_project = lexis_project
        self._lexis_project_resource_id = lexis_project_resource_id

        # Loaded by fetch_submitter_info_from_heappe method, heappe_url is needed to fetch submitter info from HEAppE
        self._accounting_string = None
        self._cluster_id = None
        self._node_type_id = None
        self._node_type_name = None

        # Loaded by fetch_assignment_data method
        self._heappe_url = None
        self._resource_name = None
        # self._lexis_resource_id = None
        self._provider_name = QAAS_PROVIDER_NAME
        self._allocation_amount = None
        # amount of currently consumpted Qseconds
        self._current_consumption = None
        self._aggregation_name = None
        self._cluster_name = None
        self._location_name = None

        self._resource_start_date = None
        self._resource_end_date = None

        # Cyclops entities IDs, loaded later by fetch_cyclops_entities_ids() method
        self._cyclops_customer_id = None
        self._cyclops_resource_id = None

    @property
    def submitter_email(self) -> str:
        return self._submitter_email

    @property
    def lexis_project(self) -> str:
        return self._lexis_project

    @property
    def accounting_string(self) -> str:
        return self._accounting_string

    @property
    def cluster_id(self) -> int:
        return self._cluster_id

    @property
    def cluster_name(self) -> str:
        return self._cluster_name

    @property
    def node_type_id(self) -> int:
        return self._node_type_id

    @property
    def node_type_name(self) -> str:
        return self._node_type_name

    @property
    def resource_name(self) -> str:
        return self._resource_name

    @property
    def location_name(self) -> str:
        return self._location_name

    @property
    def heappe_url(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: HEAppE URL specified in LEXIS resource assignment specifications, otherwise None
        """
        return self._heappe_url

    @property
    def lexis_project_resource_id(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: LEXIS resource ID loaded from LEXIS resource assignment, otherwise None
        """
        return self._lexis_project_resource_id

    @property
    def allocation_amount(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: Allocation amount specified in LEXIS resource assignment, otherwise None
        """
        return self._allocation_amount

    @property
    def current_consumption(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: Amount of currently consumpted Qseconds for the resource, otherwise None
        """
        return self._current_consumption

    @current_consumption.setter
    def current_consumption(self, consumpted_qseconds: float):
        """To set current consumption for the resource"""
        # Optional: You can add validation here since it's a setter
        if consumpted_qseconds < 0:
            raise ValueError("Consumption cannot be negative.")

        self._current_consumption = consumpted_qseconds

    @property
    def provider_name(self):
        """Geter for provider name"""
        return self._provider_name

    @property
    def aggregation_name(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: Aggregation name specified in LEXIS resource assignment, otherwise None
        """
        return self._aggregation_name

    @property
    def resource_start_date(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: Resource start date loaded from LEXIS resource assignment, otherwise None
        """
        return self._resource_start_date

    @property
    def resource_end_date(self):
        """To fetch value, call AccountingInfo.fetch_assignment_data()

        :return: Resource end date loaded from LEXIS resource assignment, otherwise None
        """
        return self._resource_end_date

    @property
    def cyclops_customer_id(self):
        """To fetch value, call AccountingInfo.fetch_cyclops_entities_ids()

        :return: Cyclops customer ID corresponding to LEXIS project, otherwise None
        """
        return self._cyclops_customer_id

    @property
    def cyclops_resource_id(self):
        """To fetch value, call AccountingInfo.fetch_cyclops_entities_ids()

        :return: Cyclops resource ID corresponding to LEXIS resource, otherwise None
        """
        return self._cyclops_resource_id

    def decode_user_jwt_identifier(self) -> str:
        """
        Decode the user JWT token and extract the user identifier.

        This method decodes the JWT token stored in ``self._user_jwt`` without
        verifying the signature and returns the identifier claim from the decoded token.

        :return: The user identifier is extracted from the JWT token, or None if not present.
        :rtype: str or "UNKNOWN"
        """
        decoded = jwt.decode(self._user_jwt, options={"verify_signature": False})
        return decoded.get("sub", "UNKNOWN")

    def decode_user_jwt_and_verify(self) -> str | bool:
        """Should decode given user JWT and compare sub attribute email inside JWT to submitter_email

        :return: Returns True if decoded JWT is valid and email matches submitter_email, otherwise False
        """
        try:
            decoded = jwt.decode(self._user_jwt, options={"verify_signature": False})
            exp_timestamp = decoded.get("exp")
            email = decoded.get("email")
            if exp_timestamp and datetime.fromtimestamp(
                exp_timestamp, tz=timezone.utc
            ) < datetime.now(timezone.utc):
                print(f"JWT of user {email} is expired", file=sys.stderr)
                return False
            return email
        except Exception as e:
            print(f"Error decoding JWT: {e}", file=sys.stderr)
            return False

    async def _internal_fetch_accounting_info_logic(self, job_id: str) -> bool:
        """Internal coroutine to handle the sequence logic."""
        # 1. Sequential Call (The Dependency)
        # Ensure this finishes first so self._heappe_url is populated
        success = await self.fetch_and_verify_assignment_data()

        if not success:
            return False

        # 2. Concurrent Calls
        # These run at the same time now that the URL is ready

        # Currently not available
        # submitter_info_task = asyncio.to_thread(self.fetch_submitter_info_from_heappe, job_id)

        return True

    def fetch_all_accounting_info(self, job_id: str) -> bool:
        """The clean public synchronous wrapper."""
        try:
            return asyncio.run(self._internal_fetch_accounting_info_logic(job_id))
        except Exception as e:
            import traceback

            traceback.print_exc(file=sys.stderr)
            # Handle or log unexpected errors during the loop
            print(f"Accounting fetch failed: {e}")
            return False

    def fetch_submitter_info_from_heappe(self, job_id: str) -> str | None:
        """Fetches submitter info from HEAppE JobReporting endpoint

        :param job_id: current HEAppE job ID
        :return: Submitter email or None if submitter info cannot be obtained for any reason (HEAppE URL not found in LEXIS assignment, failure to fetch info from HEAppE, etc.)
        """
        # if not self._heappe_url:
        #     if not asyncio.run(self.fetch_and_verify_assignment_data()):
        #         print("Unable to fetch HEAppE URL from LEXIS assignment data, cannot fetch submitter info from HEAppE", file=sys.stderr)
        #         return None

        # FIXME: Endpoint is not currently available at HEAppE!!!
        return NotImplemented
        fetch_from = (
            datetime.now(timezone.utc) - timedelta(days=1)
        )  # Fetch jobs from last 30 days to ensure we cover the submitter info for current job, even if it was submitted a while ago
        fetch_from_isoformat = fetch_from.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        try:
            report_resp = requests.get(
                self._heappe_url + "/heappe/JobReporting/JobsDetailedReport",
                params={"TimeFrom": fetch_from_isoformat},
                headers=HEAPPE_REPORTED_AUTH_HEADER,
                timeout=120,
            )
            if report_resp.status_code != 200:
                print(
                    "Warning: Request to HEAppE JobsDetailedReport endpoint FAILED!",
                    file=sys.stderr,
                    flush=True,
                )
                return None
            reported_resources_json = report_resp.json()

            # Iterate through top-level items
            for resource in reported_resources_json:
                # Access Clusters within the container
                if resource.get("Name") != self._resource_name:
                    clusters = resource.get("Clusters", [])

                    for cluster in clusters:
                        accounting_string = cluster.get("AccountingString")
                        # Access ClusterNodeTypes (Queues) within the cluster
                        node_types = cluster.get("ClusterNodeTypes", [])
                        cluster_name = cluster["Name"]
                        cluster_id = cluster["Id"]
                        for node_type in node_types:
                            jobs = node_type.get("Jobs", [])
                            node_type_name = node_type["Name"]
                            node_type_id = node_type["Id"]

                            for job in jobs:
                                if job["Id"] == job_id:
                                    self._accounting_string = accounting_string
                                    self._cluster_id = cluster_id
                                    self._cluster_name = cluster_name
                                    self._node_type_id = node_type_id
                                    self._node_type_name = node_type_name
                                    self._submitter_email = job["Submitter"]
                                    return job["Submitter"]
            return None
        except requests.exceptions.Timeout:
            print(
                "Warning: Connection to HEAppE JobsDetailedReport endpoint TIMED OUT!",
                file=sys.stderr,
                flush=True,
            )
            return None

    async def fetch_and_verify_assignment_data(self) -> str | None:
        """Fetch HEAppE URL from Specifications of lexis assignment using endpoint {LEXIS_API_URL}/userorg/api/ProjectResourceAssignment/{lexis_resource_assignment_id} and return it, so it can be used to fetch submitter info from HEAppE in future steps. If there is no such assignment or HEAppE URL is not specified, return None. Loads also lexis_resource_id from this endpoint, which is needed for verification of resource assignment in future steps.

        Also verifies name of LEXIS resource using lexis_resource_id and endpoint {QAAS_LEXIS_API_URL}/userorg/api/ProjectResource/{lexis_resource_id}

        :return: HEAppE URL or None if not found, verification fails, or error occurs
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Fetch assignment data and verify resource ID concurrently
                assignment_data, is_verified = await asyncio.gather(
                    self._fetch_assignment_data(session),
                    self._get_resource_info_and_verify_given_resource_id(session),
                    return_exceptions=True,
                )

                # Handle exceptions from gather
                if isinstance(assignment_data, Exception):
                    print(
                        f"Failed to fetch resource assignment data from LEXIS API: {assignment_data}",
                        file=sys.stderr,
                    )
                    return None
                if isinstance(is_verified, Exception):
                    print(
                        f"Error verifying resource ID with LEXIS API: {is_verified}",
                        file=sys.stderr,
                    )
                    return None

                # Validate results
                if not assignment_data or not is_verified:
                    return None

                return assignment_data

        except Exception as e:
            print(f"Error in fetch_and_verify_assignment_data: {e}", file=sys.stderr)
            return None

    async def _fetch_assignment_data(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """Fetch HEAppE URL and resource details from LEXIS assignment endpoint"""
        try:
            async with session.get(
                f"{QAAS_LEXIS_API_URL}/userorg/api/ProjectResource/{self._lexis_project_resource_id}",
                headers={"Authorization": f"Bearer {self._user_jwt}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    print(
                        f"[api/ProjectResource/{self._lexis_project_resource_id}] Status code: {resp.status}, Response: {await resp.text()}",
                        file=sys.stderr,
                    )
                    return None

                resource_data = await resp.json()
                assignment_data = resource_data.get("Assignments", [{}])[0]

                # Extract HEAppE URL from specifications
                heappe_url = None
                specifications = assignment_data.get("Specifications", [])
                for spec in specifications:
                    if spec.get("Key") == "HEAPPE_URL":
                        heappe_url = spec.get("Value")
                        break

                if not heappe_url:
                    print(
                        f"HEAppE URL not specified in resource assignment {self.lexis_project_resource_id}",
                        file=sys.stderr,
                    )
                    return None

                self._heappe_url = heappe_url
                self._allocation_amount = assignment_data.get("AllocationAmount")
                self._aggregation_name = assignment_data.get("AggregationName")
                self._location_name = assignment_data.get("LocationName")

                # Extract and cache resource details
                if not self._allocation_amount or not self._aggregation_name:
                    print(
                        "WARN! Missing allocation amount or aggregation name in resource assignment",
                        file=sys.stderr,
                    )
                    return None

                return heappe_url

        except asyncio.TimeoutError:
            print(
                "Timeout while fetching resource assignment data from LEXIS API",
                file=sys.stderr,
            )
            return None

    async def _get_resource_info_and_verify_given_resource_id(
        self, session: aiohttp.ClientSession
    ) -> bool:
        """Verify name of LEXIS resource and assignment ID and fetch resource name"""
        try:
            async with session.get(
                f"{QAAS_LEXIS_API_URL}/userorg/api/ProjectResource",
                params={
                    "ProjectShortName": self._lexis_project,
                    "LocationTypeId": 7,
                },  # LocationType Quantum = 7
                headers={"Authorization": f"Bearer {self._user_jwt}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    print(
                        f"[api/ProjectResource] Status code: {resp.status}, Response: {await resp.text()}",
                        file=sys.stderr,
                    )
                    return False

                project_resources: list[dict] = await resp.json()

                # Find Resource
                project_resource = None
                for pr in project_resources:
                    if pr["Id"] == self._lexis_project_resource_id:
                        project_resource = pr
                if (
                    project_resource is None
                    or len(project_resource["Assignments"]) == 0
                ):
                    print(
                        f"Resource ID {self._lexis_project_resource_id} with assignment not found in LEXIS project {self._lexis_project}",
                        file=sys.stderr,
                    )
                    return False

                self._resource_name = project_resource.get("Name")
                self._resource_start_date = project_resource.get("StartDate")
                self._resource_end_date = project_resource.get("EndDate")

                return True

        except asyncio.TimeoutError:
            print("Timeout while verifying resource ID with LEXIS API", file=sys.stderr)
            return False
