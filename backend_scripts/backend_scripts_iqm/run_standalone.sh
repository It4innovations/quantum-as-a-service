#!/bin/bash

# run_standalone.sh - Script dispatcher of standalone circuit run. Executed by HEAppE directly

set -e  # Exit on any error


# TRANSPILE AND RUN KWARGS
export RUN_SHOTS="$1"

# SET IQM server URL
export IQM_SERVER_URL="https://cocos.vlq.it4i.cz:5584/star24"


# Print info about what we're running
echo "Running command: Standalone transpile and run of circuit"
echo "Script path: $SCRIPT_PATH"
echo "Working directory: $(pwd)"

# Run the selected Python script with all environment variables preserved
exec python3.11 /absolute_path/backend_scripts/run_standalone.py