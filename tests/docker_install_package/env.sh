# Sourced by run.sh and run_local_dist.sh.
# load_env FILE: reads KEY=VALUE lines of a dotenv file (the repo .env used by pytest),
# strips surrounding quotes, and fills DOCKER_ENV with "-e NAME" for the variables the
# smoke test reads. Values stay in the environment of the docker command only; they are
# not written to the image. SHOTS and SKIP_QPU set in the shell override the file.

SMOKE_VARS=(
  LEXIS_TOKEN QHEAPPE_PROJECT QHEAPPE_RESOURCE QHEAPPE_BACKEND QHEAPPE_LEXIS_API_URL
  ACCESS_TOKEN LEXIS_PROJECT RESOURCE_NAME BACKEND_NAME LEXIS_API_URL
  SHOTS SKIP_QPU
)

load_env() {
  local file="$1" line key value shell_shots="${SHOTS-}" shell_skip="${SKIP_QPU-}"
  [[ -f "$file" ]] || { echo "Env file not found: $file" >&2; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]] || continue
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    [[ " ${SMOKE_VARS[*]} " == *" $key "* ]] || continue
    if [[ "$value" =~ ^\"(.*)\"[[:space:]]*$ || "$value" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%%[[:space:]]#*}"  # unquoted: drop a trailing " # comment"
      value="${value%"${value##*[![:space:]]}"}"
    fi
    export "$key=$value"
  done <"$file"
  [[ -n "$shell_shots" ]] && export SHOTS="$shell_shots"
  [[ -n "$shell_skip" ]] && export SKIP_QPU="$shell_skip"

  DOCKER_ENV=()
  local name
  for name in "${SMOKE_VARS[@]}"; do
    [[ -n "${!name-}" ]] && DOCKER_ENV+=(-e "$name")
  done
  return 0
}
