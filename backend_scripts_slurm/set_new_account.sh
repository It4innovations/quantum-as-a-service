#!/bin/bash

# Check if account parameter is provided
if [ $# -ne 1 ]; then
    echo "Usage: $0 <account>"
    exit 1
fi

ACCOUNT=$1

# Create account (if doesn't exist)
echo y | sacctmgr add account "$ACCOUNT"

# Add user to account
echo y | sacctmgr add user qaas_user account="$ACCOUNT"

# Set QOS for the account (non-interactive)
echo y | sacctmgr modify account "$ACCOUNT" set qos=init_qos,compute_qos

echo "Successfully configured account $ACCOUNT for user qaas_user with QOS: init_qos, compute_qos"