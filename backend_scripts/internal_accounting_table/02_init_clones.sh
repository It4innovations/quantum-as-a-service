#!/bin/bash
set -e

# 1. Feed the schema file into the default 'dev_db' database
echo "Initializing master template schema in $POSTGRES_DB..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/01_init.sql

# List of databases to clone dev_db into
DATABASES=(
    "qaas_dev_lexis_prod"
    "qaas_dev_lexis_test"
    "qaas_test_lexis_test"
    "qaas_test_lexis_prod"
)

echo "Preparing $POSTGRES_DB for cloning (terminating other connections)..."

# 2. Terminate any active connections to dev_db and temporarily block new ones
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<EOSQL
  -- Revoke connect privileges temporarily so no background process can sneak in
  ALTER DATABASE "$POSTGRES_DB" ALLOW_CONNECTIONS false;
  
  -- Forcefully disconnect anyone currently sitting in dev_db
  SELECT pg_terminate_backend(pid) 
  FROM pg_stat_activity 
  WHERE datname = '$POSTGRES_DB' AND pid <> pg_backend_pid();
EOSQL

# 3. Clone the template database into each target name
for db in "${DATABASES[@]}"; do
    echo "Cloning $POSTGRES_DB into new database: $db"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" -c "CREATE DATABASE $db TEMPLATE $POSTGRES_DB;"
done

# 4. Re-allow connections to dev_db so it's usable again
echo "Re-enabling connections to $POSTGRES_DB..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" -c "ALTER DATABASE \"$POSTGRES_DB\" ALLOW_CONNECTIONS true;"