# Clean install of qaas as documented in README.rst - no repo sources are copied in.
FROM python:3.12-slim

# git: py4lexis 7.1.x depends on python-http-irods-client via a git URL
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Empty QAAS_VERSION (default) installs the latest release
ARG QAAS_VERSION=
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

RUN pip install "qaas${QAAS_VERSION:+==$QAAS_VERSION}" \
    --extra-index-url https://opencode.it4i.eu/api/v4/projects/107/packages/pypi/simple

WORKDIR /app
COPY smoke_submit.py .
CMD ["python", "smoke_submit.py"]
