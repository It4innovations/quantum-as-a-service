#!/bin/bash

# run.sh - Script dispatcher for quantum backend operations. Executed by HEAppE directly.

set -e  # Exit on any error

# Log file for execution times
LOG_FILE="execution_time_log.txt"

# Check if Q_COMMAND is set
if [ -z "$Q_COMMAND" ]; then
    echo "Error: Q_COMMAND environment variable is required"
    echo "Valid values: backend_init, backend_run, pulla_init, pulla_submit_playlist, get_callibration_set"
    exit 1
fi

# Default socket path
SOCKET_PATH="${IQM_SERVICE_SOCKET:-/var/run/iqm/backend.sock}"

# Usage function
usage() {
    cat << EOF
Usage:

Commands (via Q_COMMAND env var):
  backend_init          Initialize backend for task
  backend_run           Run quantum job for task
  pulla_init            Initialize pulla instance for task
  pulla_submit_playlist Submit Pulla Playlist (execution schedule defined by low-level pulse control)
  get_calibration_set   Get calibration set by UUID or default one

Environment:
  Q_COMMAND             Command to execute (required)
  Q_OPTIONAL_ARG        Optional Args to be passed to command
  IQM_SERVICE_SOCKET    Socket path (default: /var/run/iqm/backend.sock)

The script extracts job_id and task_id from the current working directory.
Expected path structure: .../job_id/task_id/
EOF
    exit 1
}

# Extract job_id and task_id from current directory
# Get absolute path
CURRENT_DIR=$(pwd)

# Get last two directory components
# Example: /path/to/job_123/task_456 -> job_123/task_456
TASK_ID=$(basename "$CURRENT_DIR")
JOB_ID=$(basename "$(dirname "$CURRENT_DIR")")

# Combine as job_id/task_id
FULL_TASK_ID="${JOB_ID}/${TASK_ID}"


# Validate that we have both IDs
if [ -z "$JOB_ID" ] || [ -z "$TASK_ID" ]; then
    echo "Error: Could not extract job_id and task_id from current directory"
    echo "Current directory: $CURRENT_DIR"
    echo "Expected structure: .../job_id/task_id/"
    exit 1
fi

# Validate command
if [[ "$Q_COMMAND" != "backend_init" 
    && "$Q_COMMAND" != "backend_run" 
    && "$Q_COMMAND" != "pulla_init" 
    && "$Q_COMMAND" != "pulla_submit_playlist" 
    && "$Q_COMMAND" != "get_calibration_set"
    && "$Q_COMMAND" != "get_dynamic_quantum_architecture"
    ]]; then
    echo "Error: Invalid command '$Q_COMMAND'"
    usage
fi

# Check if socket exists
if [ ! -S "$SOCKET_PATH" ]; then
    echo "Error: Socket not found at $SOCKET_PATH"
    echo "Is the IQM backend service running?"
    exit 1
fi

# Start timing
START_TIME=$(date +%s.%N)
START_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "Sending: $Q_COMMAND $FULL_TASK_ID $Q_OPTIONAL_ARG"

# 60s timeout for socket communication
RESPONSE=$(echo "$Q_COMMAND $FULL_TASK_ID $Q_OPTIONAL_ARG" | nc -U -w 60 "$SOCKET_PATH")
EXIT_CODE=$?

# End timing
END_TIME=$(date +%s.%N)
END_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Calculate duration
DURATION=$(echo "$END_TIME - $START_TIME" | bc)

# Log execution time
LOG_ENTRY="$START_TIMESTAMP | $Q_COMMAND | $END_TIMESTAMP | ${DURATION} | $FULL_TASK_ID | $EXIT_CODE"
echo "$LOG_ENTRY" >> "$LOG_FILE"

if [ $EXIT_CODE -ne 0 ]; then
    echo "Error: Failed to communicate with service (exit code: $EXIT_CODE)"
    echo "Execution time: ${DURATION}s"
    exit 1
fi

echo "Response: $RESPONSE"
echo "Execution time: ${DURATION}s"

# Check response
if [[ "$RESPONSE" == "DONE" ]]; then
    echo "Success: Command completed"
    exit 0
elif [[ "$RESPONSE" == ERROR:* ]]; then
    echo "Error: Service returned error"
    exit 1
else
    echo "Warning: Unexpected response"
    exit 1
fi