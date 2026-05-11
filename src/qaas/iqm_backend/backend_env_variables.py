import os
import sys
from datetime import timedelta

print("Loading environment variables...")

QAAS_ALLOWED_CLIENT_COUNT = int(os.getenv('QAAS_ALLOWED_CLIENT_COUNT',"100"))
print(f"Will accept {QAAS_ALLOWED_CLIENT_COUNT} client at max")

DEFAULT_EXECUTION_WALLTIME=7200 #seconds
ALLOWED_EXECUTION_SHARING=4
computed_fetch_margin=timedelta(seconds=ALLOWED_EXECUTION_SHARING*DEFAULT_EXECUTION_WALLTIME+10) # 10s is safety margin for time drift

QAAS_LEXIS_API_URL = os.getenv('QAAS_LEXIS_API_URL', 'https://api.lexis.tech')

# QAAS_HEAPPE_URL = os.getenv('QAAS_HEAPPE_URL') # NOTE: Current issue: /heappe/heappe/ (douple postfix)
QAAS_REPORTER_USER = os.getenv('QAAS_HEAPPE_REPORTER_ACCOUNT', 'sys_reporter')
QAAS_REPORTER_PWD = os.getenv('QAAS_HEAPPE_REPORTER_ACCOUNT_PWD', default='NONE!!!')
if QAAS_REPORTER_PWD == "NONE!!!":
    print("WARNING: NO QAAS REPORTER PASSWORD PROVIDED!!!", file=sys.stderr, flush=True)
HEAPPE_REPORTED_AUTH_HEADER = {'X-API-Key':QAAS_REPORTER_USER+':'+QAAS_REPORTER_PWD}

CYCLOPS_KAFKA_SERVER = os.getenv('CYCLOPS_KAFKA_SERVER')
CYCLOPS_API_URL = os.getenv('CYCLOPS_API_URL')
CYCLOPS_API_KEY = os.getenv('CYCLOPS_API_KEY')
CYCLOPS_DEFAULT_UNIT = os.getenv('CYCLOPS_ACCOUNTING_UNIT', 'quantum-seconds')
CYCLOPS_DEFAULT_TIMEOUT = int(os.getenv('CYCLOPS_DEFAULT_TIMEOUT', "3600"))
CYCLOPS_DEFAULT_RETRIES = int(os.getenv('CYCLOPS_DEFAULT_RETRIES', "3"))
CYCLOPS_DEFAULT_TOPIC = os.getenv('CYCLOPS_DEFAULT_TOPIC', "UDR")


# check all required environment variables at the start of the service, so if something is missing, it fails immediately
if not CYCLOPS_API_URL:
    print("CYCLOPS_API_URL environment variable is required to fetch Cyclops entity IDs", file=sys.stderr)
    exit(-1)
if not CYCLOPS_KAFKA_SERVER:
    print("CYCLOPS_KAFKA_SERVER environment variable is required to fetch Cyclops entity IDs", file=sys.stderr)
    exit(-2)
if not QAAS_LEXIS_API_URL:
    print("QAAS_LEXIS_API_URL environment variable is required to fetch LEXIS entity IDs", file=sys.stderr)
    exit(-3)

print("All required environment variables are set. Starting service...")
    