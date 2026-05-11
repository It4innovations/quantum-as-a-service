import sys
from datetime import datetime, timezone, timedelta
from typing import Tuple
import aiohttp
import asyncio
import jwt
import requests

from qaas.iqm_backend.backend_env_variables import (
    CYCLOPS_API_URL,
    CYCLOPS_API_KEY,
    HEAPPE_REPORTED_AUTH_HEADER,
    QAAS_LEXIS_API_URL
)
    
class AccountingInfo:
    """
    Info about submitting user and used resources, needed for accounting record in Cyclops.
    """
    def __init__(self, user_jwt:bytes, submitter_email:str, lexis_project:str, lexis_project_resource_id:str):
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
        self._allocation_amount = None
        self._aggregation_name = None
        self._cluster_name = None
        self._location_name = None
        
        self._resource_start_date = None
        self._resource_end_date = None

        # Cyclops entities IDs, loaded later by fetch_cyclops_entities_ids() method
        self._cyclops_customer_id = None
        self._cyclops_resource_id = None
        
        
    @property
    def submitter_email(self)->str:
        return self._submitter_email
    @property
    def lexis_project(self)->str:
        return self._lexis_project
    @property
    def accounting_string(self)->str:
        return self._accounting_string
    @property
    def cluster_id(self)->int:
        return self._cluster_id
    @property
    def cluster_name(self)->str:
        return self._cluster_name
    @property
    def node_type_id(self)->int:
        return self._node_type_id
    @property
    def node_type_name(self)->str:
        return self._node_type_name
    @property
    def resource_name(self)->str:
        return self._resource_name
    @property
    def location_name(self)->str:
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
    
    def decode_user_jwt_email(self)->str:
        """
        Decode the user JWT token and extract the email address.

        This method decodes the JWT token stored in ``self._user_jwt`` without
        verifying the signature and returns the email claim from the decoded token.

        :return: The user identifier is extracted from the JWT token, or None if not present.
        :rtype: str or "UNKNOWN"
        """
        decoded = jwt.decode(self._user_jwt, options={"verify_signature": False})
        return decoded.get('sub', "UNKNOWN")
    
    def decode_user_jwt_and_verify(self)->str|bool:
        """Should decode given user JWT and compare sub attribute email inside JWT to submitter_email

        :return: Returns True if decoded JWT is valid and email matches submitter_email, otherwise False
        """
        try:
            decoded = jwt.decode(self._user_jwt, options={"verify_signature": False})
            exp_timestamp = decoded.get('exp')
            email = decoded.get('email')
            if exp_timestamp and datetime.fromtimestamp(exp_timestamp, tz=timezone.utc) < datetime.now(timezone.utc):
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
        results = await asyncio.gather(
            asyncio.to_thread(self.fetch_submitter_info_from_heappe, job_id),
            self.fetch_cyclops_entities_ids(),
            return_exceptions=True
        )

        # 3. Validation
        # Check if any exceptions occurred or if None was returned
        return all(res is not None and not isinstance(res, Exception) for res in results)

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
        
        #FIXME: Endpoint is not currently available at HEAppE!!!
        return NotImplemented
        fetch_from = datetime.now(timezone.utc) - timedelta(days=1)  # Fetch jobs from last 30 days to ensure we cover the submitter info for current job, even if it was submitted a while ago
        fetch_from_isoformat = fetch_from.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        try:
            report_resp = requests.get(
                self._heappe_url+"/heappe/JobReporting/JobsDetailedReport",
                params={'TimeFrom': fetch_from_isoformat},
                headers=HEAPPE_REPORTED_AUTH_HEADER,
                timeout=120)
            if report_resp.status_code != 200:
                print("Warning: Request to HEAppE JobsDetailedReport endpoint FAILED!", file=sys.stderr, flush=True)
                return None
            reported_resources_json = report_resp.json()
                
            # Iterate through top-level items
            for resource in reported_resources_json:
                # Access Clusters within the container
                if resource.get('Name') != self._resource_name:
                    clusters = resource.get('Clusters', [])

                    accounting_string = cluster.get('AccountingString')
                    for cluster in clusters:
                        # Access ClusterNodeTypes (Queues) within the cluster
                        node_types = cluster.get('ClusterNodeTypes', [])
                        cluster_name = cluster['Name']
                        cluster_id = cluster['Id']
                        for node_type in node_types:
                            jobs = node_type.get('Jobs', [])
                            node_type_name = node_type['Name']
                            node_type_id = node_type['Id']
                            
                            for job in jobs:
                                if job['Id'] == job_id:
                                    self._accounting_string = accounting_string
                                    self._cluster_id = cluster_id
                                    self._cluster_name = cluster_name
                                    self._node_type_id = node_type_id
                                    self._node_type_name = node_type_name
                                    self._submitter_email = job['Submitter']
                                    return job['Submitter']
            return None
        except requests.exceptions.Timeout:
            print("Warning: Connection to HEAppE JobsDetailedReport endpoint TIMED OUT!", file=sys.stderr, flush=True)
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
                    return_exceptions=True
                )
                
                # Handle exceptions from gather
                if isinstance(assignment_data, Exception):
                    print(f"Failed to fetch resource assignment data from LEXIS API: {assignment_data}", file=sys.stderr)
                    return None
                if isinstance(is_verified, Exception):
                    print(f"Error verifying resource ID with LEXIS API: {is_verified}", file=sys.stderr)
                    return None
                
                # Validate results
                if not assignment_data or not is_verified:
                    return None
                
                return assignment_data
        
        except Exception as e:
            print(f"Error in fetch_and_verify_assignment_data: {e}", file=sys.stderr)
            return None

    async def _fetch_assignment_data(self, session: aiohttp.ClientSession) -> str | None:
        """Fetch HEAppE URL and resource details from LEXIS assignment endpoint"""
        try:
            async with session.get(
                f"{QAAS_LEXIS_API_URL}/userorg/api/ProjectResourceAssignment/{self._lexis_project_resource_id}",
                headers={"Authorization": f"Bearer {self._user_jwt}"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"Status code: {resp.status}, Response: {await resp.text()}")
                
                assignment_data = await resp.json()
                
                # Extract HEAppE URL from specifications
                heappe_url = None
                specifications = assignment_data.get('Specifications', [])
                for spec in specifications:
                    if spec.get('Key') == 'HEAPPE_URL':
                        heappe_url = spec.get('Value')
                        break
                
                if not heappe_url:
                    raise Exception(f"HEAppE URL not specified in resource assignment {self.lexis_project_resource_id}")
                
                
                self._heappe_url = heappe_url
                self._allocation_amount = assignment_data.get('AllocationAmount')
                self._aggregation_name = assignment_data.get('AggregationName')
                self._location_name = assignment_data.get('LocationName')
                
                # Extract and cache resource details
                if not self._allocation_amount or not self._aggregation_name:
                    raise Exception("Missing allocation amount or aggregation name in resource assignment")
                
                return heappe_url
        
        except asyncio.TimeoutError:
            raise Exception("Timeout while fetching resource assignment data from LEXIS API")

    async def _get_resource_info_and_verify_given_resource_id(self, session: aiohttp.ClientSession) -> bool:
        """Verify name of LEXIS resource and assignment ID and fetch resource name"""
        try:
            async with session.get(
                f"{QAAS_LEXIS_API_URL}/userorg/api/ProjectResource",
                params={"ProjectShortName": self._lexis_project, "LocationTypeId": 7}, # LocationType Quantum = 7
                headers={"Authorization": f"Bearer {self._user_jwt}"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"Status code: {resp.status}, Response: {await resp.text()}")
                
                project_resources:list[dict] = await resp.json()
                
                # Find Resource
                project_resource=None
                for pr in project_resources:
                    if pr["Id"] == self._lexis_project_resource_id:
                        project_resource = pr
                if project_resource is None or len(project_resource["Assignments"]) == 0:
                    raise Exception(f"Resource ID {self._lexis_project_resource_id} with assignment not found in LEXIS project {self._lexis_project}")
                
                self._resource_name = project_resource.get('Name')
                self._resource_start_date = project_resource.get('StartDate')
                self._resource_end_date = project_resource.get('EndDate')
                
                return True
        
        except asyncio.TimeoutError:
            raise Exception("Timeout while verifying resource ID with LEXIS API")

    
    async def fetch_cyclops_entities_ids(self) -> Tuple[str, str] | None:
        """ From Cyclops's Customer DB API fetches cyclops customer_id, which is equal to lexis project short name, and resource_id, which is needed for accounting record in Cyclops, using lexis project and resource name as reference. Using endpoint CYCLOPS_API_URL/customerdbAPI/api/v1.0/customer?search={lexis_short_name} and CYCLOPS_API_URL/planmanagerAPI/api/v1.0/plan.
        
        Sets also self._cyclops_customer_id and self._cyclops_resource_id, which are needed for future accounting records, so they are loaded only once and then cached in the instance.
        :return: Tuple of (cyclops_customer_id, cyclops_resource_id) or None if error occurs or entities not found
        """
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Fetch Customer ID first
                customer_id = await self._fetch_cyclops_customer_id(session)
                if not customer_id:
                    return None
                
                # Cache it so the next method can use it
                self._cyclops_customer_id = customer_id
                
                # 2. Now fetch Plan ID using the cached customer_id
                resource_id = await self._fetch_cyclops_plan_id(session)
                if not resource_id:
                    return None
                
                self._cyclops_resource_id = resource_id
                return str(customer_id), str(resource_id)
                
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            print(f"Error fetching Cyclops entity IDs: {e}", file=sys.stderr)
            return None

    async def _fetch_cyclops_customer_id(self, session: aiohttp.ClientSession) -> str | None:
        """ Fetch customer ID from Cyclops Customer DB API """
        try:
            async with session.get(
                f"{CYCLOPS_API_URL}/customerdbAPI/api/v1.0/customer",
                params={"search": self._lexis_project},
                headers={'X-API-KEY': CYCLOPS_API_KEY},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp_customer:
                if resp_customer.status != 200:
                    raise Exception(f"Status code: {resp_customer.status}, Response: {await resp_customer.text()}")
                
                customers = await resp_customer.json()
                cyclops_customer_id = None
                
                # for debug:
                # print("customers: "+str(customers.get("customers",[])), file=sys.stderr, flush=True)
                
                for customer in customers.get("customers",[]):
                    if customer.get('Name') == self._lexis_project:
                        cyclops_customer_id = customer.get('CustomerId')
                        break
                
                if not cyclops_customer_id:
                    raise Exception(f"Customer with name {self._lexis_project} not found in Cyclops")
                
                return cyclops_customer_id
        
        except asyncio.TimeoutError:
            raise Exception("Timeout while fetching customer data from Cyclops API")

    async def _fetch_cyclops_plan_id(self, session: aiohttp.ClientSession) -> str | None:
        """ Fetch resource entity details (plan ID in Cyclops) """
        try:
            async with session.get(
                f"{CYCLOPS_API_URL}/planmanagerAPI/api/v1.0/plan",
                # params={"customerId": getattr(self, '_cyclops_customer_id', '')},
                headers={'X-API-KEY': CYCLOPS_API_KEY},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp_plan:
                if resp_plan.status != 200:
                    raise Exception(f"Status code: {resp_plan.status}, Response: {await resp_plan.text()}")
                
                plans = await resp_plan.json()
                cyclops_resource_id = None
                
                # for debug:
                # print("plans: "+str(plans), file=sys.stderr, flush=True)
                
                for plan in plans:
                    if plan.get('Name') == self._location_name+'_'+self._resource_name:
                        cyclops_resource_id = plan.get('ID')
                        return cyclops_resource_id
                
                raise Exception(f"Plan with name {self._resource_name} not found for customer {self._lexis_project} in Cyclops")
        
        except asyncio.TimeoutError:
            raise Exception("Timeout while fetching plan data from Cyclops API")
