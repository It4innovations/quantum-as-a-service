"""Configuration of the live test suite, read from environment (or .env via pytest-dotenv).

  LEXIS_TOKEN              pre-obtained LEXIS access token (LexisSession login is interactive)
  QHEAPPE_PROJECT          LEXIS project short name, e.g. vlq_qaas_project
  QHEAPPE_RESOURCE         LEXIS quantum resource, e.g. VLQ-CZ-dev_qshceduler-alpha
  QHEAPPE_BACKEND          backend (cluster) name, e.g. VLQ; optional for single-cluster resources
  QHEAPPE_LEXIS_API_URL    LEXIS API base URL (default production)
  QHEAPPE_INVALID_RESOURCE known-bad resource name (negative tests)
  QHEAPPE_CLIENT_ARGS      JSON dict of extra QClient kwargs, e.g. {"load_machine_info": false}
  QHEAPPE_MIN_FIDELITY     threshold for Bell/zero-state checks; set 0 for mock backends
  QHEAPPE_JOB_TIMEOUT      per-test timeout for tests that wait on jobs (seconds)
"""

import json
import os

TOKEN = os.environ.get("LEXIS_TOKEN")
PROJECT = os.environ.get("QHEAPPE_PROJECT")
RESOURCE = os.environ.get("QHEAPPE_RESOURCE")
BACKEND_NAME = os.environ.get("QHEAPPE_BACKEND") or None
LEXIS_API_URL = os.environ.get("QHEAPPE_LEXIS_API_URL", "https://api.lexis.tech/")
INVALID_RESOURCE = os.environ.get("QHEAPPE_INVALID_RESOURCE", "NON-EXISTENT-0000")
CLIENT_ARGS = json.loads(os.environ.get("QHEAPPE_CLIENT_ARGS") or "{}")
MIN_FIDELITY = float(os.environ.get("QHEAPPE_MIN_FIDELITY", "0.85"))
JOB_TIMEOUT = int(os.environ.get("QHEAPPE_JOB_TIMEOUT", "600"))

LIVE = bool(TOKEN and PROJECT and RESOURCE)
SKIP_REASON = "LEXIS_TOKEN / QHEAPPE_PROJECT / QHEAPPE_RESOURCE not set"
