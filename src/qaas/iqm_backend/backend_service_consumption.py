import threading
import sys
from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

from kafka import KafkaProducer

from qaas.iqm_backend.backend_env_variables import (
    CYCLOPS_API_URL,
    CYCLOPS_API_KEY,
    CYCLOPS_DEFAULT_TOPIC,
    CYCLOPS_DEFAULT_TIMEOUT,
    CYCLOPS_DEFAULT_RETRIES,
    CYCLOPS_KAFKA_SERVER,
    CYCLOPS_DEFAULT_UNIT
)
from qaas.iqm_backend.backend_service_accounting_info import AccountingInfo

def initializeKafkaProducer() -> KafkaProducer:
    """Initializes Kafka producer for CYCLOPS billing records

    :return: KafkaProducer instance
    """
    return KafkaProducer(
            bootstrap_servers=CYCLOPS_KAFKA_SERVER,
            value_serializer=lambda v: kafka_value_serializer(
                cyclops_resource_id=v["cyclops_resource_id"],
                usage=v["usage"],
                usage_timestamp=v["usage_timestamp"],
                lexis_resource_name=v["lexis_resource_name"],
                lexis_location_name=v["lexis_location_name"],
                lexis_project=v["lexis_project"],
                customer_id=v["customer_id"],
                submitter_email=v["submitter_email"]
                ),
            request_timeout_ms=CYCLOPS_DEFAULT_TIMEOUT,
            retries=CYCLOPS_DEFAULT_RETRIES,
        )

def kafka_value_serializer(cyclops_resource_id:int, usage:float, usage_timestamp:float, lexis_project:str, lexis_resource_name:str, lexis_location_name:str, customer_id:str, submitter_email:str)->bytes:
    """Serializer for accounting record for Kafka (part of Cyclops billing system).
    
    See `Cyclops Metric Documentation <https://cyclops-for-hpc.readthedocs.io/en/latest/metric.html>`_.

    :param cyclops_resource_id: LEXIS Resource identifier in CYCLOPS system (UUID, e.g., "d290f1ee-6c54-4b01-90e6-d701748f0851")
    :param usage: Accounted usage value
    :param usage_timestamp: UTC timestamp when the usage was recorded (datetime as float)
    :param lexis_project: LEXIS Project short name (e.g., "test_project_1")
    :param lexis_resource_name: LEXIS resource name (e.g., "VLQ-CZ")
    :param lexis_location_name: To address uniqueness of LEXIS resource name, location name should be present
    :param customer_id: CYCLOPS customer identifier (UUID, e.g., "ccc4dea0-d1d6-4a4c-bc71-3a46f1961c2a")
    :param submitter_email: Email of the user submitting the usage record
    :return: Serialized JSON value as bytes with the following structure:
        
        * Account: Customer identifier
        * Metadata: JSON object containing LexisProject, LexisResourceName, Submitter, and UDRMode
        * ResourceType: SKU name
        * ResourceId: CYCLOPS resource identifier
        * Time: Unix timestamp (integer)
        * Unit: Measurement unit
        * Usage: Usage value (float)
    """

    return json.dumps({
    "Account": customer_id, # currently equal to lexis project and its identifier in CYCLOPS system -- uuid, e.g. "ccc4dea0-d1d6-4a4c-bc71-3a46f1961c2a"
    "Metadata": {
                "LexisProject": lexis_project, # short name of the LEXIS project, e.g. "test_project_1"
                "LexisLocationName": lexis_location_name, # e.g. VLQ, Karolina
                "LexisResourceName": lexis_resource_name, # e.g. VLQ-CZ
                "Submitter": submitter_email,
                "UDRMode": "sum"
                },
    "ResourceType": lexis_location_name, # SKU name
    "ResourceId": cyclops_resource_id, # LEXIS Resource identifier in CYCLOPS system -- uuid, e.g. "d290f1ee-6c54-4b01-90e6-d701748f0851" (plan id in cyclops)
    "Time": int(usage_timestamp),
    "Unit": CYCLOPS_DEFAULT_UNIT,
    "Usage": usage
    }).encode("utf-8")

def fetch_current_resource_consumption(accounting_info: AccountingInfo) -> bool|float|str:
    """
    Fetch current resource consumption by processing each month in a separate threads from CYCLOPS's UDR api.
    
    :returns: Returns consumption (consumption>=0.0)
    :raises RuntimeError: On any error
    """
    
    start_date = datetime.fromisoformat(accounting_info.resource_start_date)
    end_date = datetime.now(datetime.now().astimezone().tzinfo)  # Current time with timezone
    
    # Generate list of month intervals
    month_intervals = _generate_month_intervals(start_date, end_date)
    
    if not month_intervals:
        return 0.0
    
    current_usage_sum = 0.0
    lock = threading.Lock()
    
    # Process each month in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(month_intervals), 10)) as executor:
        futures = {
            executor.submit(
                _fetch_and_calculate_usage,
                month_start,
                month_end,
                accounting_info.cyclops_resource_id,
                accounting_info.resource_name,
                accounting_info.location_name
            ): (month_start, month_end)
            for month_start, month_end in month_intervals
        }
        
        for future in as_completed(futures):
            month_start, month_end = futures[future]
            try:
                usage = future.result()
                if usage is None:  # API error occurred
                    print(f"Unable to fetch usage data for month {month_start} - {month_end} for cyclops plan (resource) {accounting_info.cyclops_resource_id}", file=sys.stderr, flush=True)
                    raise RuntimeError(f"Unable to fetch usage data for month {month_start} - {month_end}")   # Allow job if we cannot fetch data
                
                with lock:
                    current_usage_sum += usage
            
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.stderr)
                print(f"Error processing month {month_start} to {month_end}: {e}", file=sys.stderr)
                raise RuntimeError(f"Error processing month {month_start} to {month_end}") from e  # Deny job on error
    
    print(f"Consumption: {current_usage_sum} -- { current_usage_sum <= accounting_info.allocation_amount }; Allocation: {accounting_info.allocation_amount}", file=sys.stderr, flush=True)
    # Check if usage exceeds allocation
    return current_usage_sum

def _generate_month_intervals(start_date: datetime, end_date: datetime) -> list[tuple[datetime, datetime]]:
    """Generate list of month intervals from start_date to end_date"""
    
    # 1. Normalize: If a date is naive, assume it's UTC. 
    # If it's already aware, keep it as is (or convert to UTC).
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    intervals = []
    
    # .replace() preserves the tzinfo of the original object
    current = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    while current < end_date:
        # Calculate first day of next month
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        
        # 2. Both current and end_date are now Aware, so min() will work
        month_end = min(next_month - timedelta(seconds=1), end_date)
        
        intervals.append((current, month_end))
        current = next_month
    
    return intervals

def _fetch_and_calculate_usage(
    time_from: datetime,
    time_to: datetime,
    resource_id: str,
    lexis_resource_name: str,
    lexis_location_name: str
) -> float | None:
    """Fetch usage data for a specific time period and calculate total usage"""
    try:
        time_from_iso = _format_iso_date(time_from)
        time_to_iso = _format_iso_date(time_to)
        
        if not time_from_iso or not time_to_iso:
            return None
        
        response = requests.get(
            f"{CYCLOPS_API_URL}/udrAPI/api/v1.0/usage",
            headers={'X-API-KEY': CYCLOPS_API_KEY},
            params={"from": time_from_iso, "to": time_to_iso},
            timeout=30
        )
        
        if response.status_code != 200:
            print(f"Warning: Failed to fetch usage for {time_from_iso} to {time_to_iso}. Status code: {response.status_code}", file=sys.stderr)
            return None
        
        usage_data = response.json()
        return _calculate_resource_usage(usage_data, resource_id, lexis_resource_name, lexis_location_name)
    
    except Exception as e:
        print(f"Error fetching usage data for {time_from} to {time_to}: {e}", file=sys.stderr)
        return None

def _format_iso_date(date: datetime) -> str | None:
    """Convert datetime to ISO format with milliseconds and Z suffix"""
    try:
        return date.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, AttributeError):
        return None

def _calculate_resource_usage(usage_data: list, resource_id: str, lexis_resource_name: str, lexis_location_name: str) -> float:
    """Calculate total resource usage for specific resource and aggregation"""
    current_usage_sum = 0.0
    
    for usage_aggregation in usage_data:
        usage_data = usage_aggregation.get('Usage', None)
        if not usage_data:
            continue
        for usage_record in usage_data:
            if usage_record.get('ResourceId') == resource_id:
                metadata = usage_record.get('Metadata', {})
                lexis_resource_name_meta = metadata.get("LexisResourceName")
                lexis_location_name_meta = metadata.get("LexisLocationName")
                if lexis_resource_name and lexis_resource_name == lexis_resource_name_meta and lexis_location_name == lexis_location_name_meta:
                    current_usage_sum += usage_record.get('UsageBreakup', {}).get('used', 0.0)
    
    return current_usage_sum

def record_consumption_usage(kafka_producer: KafkaProducer, accounting_info: AccountingInfo, usage: float):
    """Records usage on CYCLOPS's Kafka (see function kafka_value_serializer)

    :param task_id: HEAppE Task identifier
    """
    
    # Inputs for serializing function (kafka_value_serializer)
    record = {
        "submitter_email": accounting_info.decode_user_jwt_email(),
        "customer_id": accounting_info.cyclops_customer_id,
        "lexis_project": accounting_info.lexis_project,
        "lexis_resource_name": accounting_info.resource_name,
        "lexis_location_name": accounting_info.location_name,
        "cyclops_resource_id": accounting_info.cyclops_resource_id,
        "usage_timestamp": datetime.now(timezone.utc).timestamp(),
        "usage": usage
    }
    
    
    kafka_producer.send(CYCLOPS_DEFAULT_TOPIC,record)