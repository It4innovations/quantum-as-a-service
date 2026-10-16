import pytest
from qiskit import QuantumCircuit
from settings import (
    BACKEND_NAME,
    CLIENT_ARGS,
    LEXIS_API_URL,
    LIVE,
    PROJECT,
    RESOURCE,
    TOKEN,
)


def make_provider(project=PROJECT, token=TOKEN):
    from qheappe_client import QProvider

    return QProvider(lexis_project=project, token=token, lexis_api_url=LEXIS_API_URL)


def close_provider(provider) -> None:
    # Clients are cached twice (None/auto and explicit name); close each once.
    for client in {id(c): c for c in provider._clients.values()}.values():
        client.close()
    provider._clients.clear()


# ---------- live fixtures ----------


@pytest.fixture(scope="session")
def provider():
    if not LIVE:
        pytest.skip("live configuration missing")
    p = make_provider()
    yield p
    close_provider(p)


@pytest.fixture(scope="session")
def backend(provider):
    return provider.get_backend(
        lexis_resource=RESOURCE, backend_name=BACKEND_NAME, **CLIENT_ARGS
    )


@pytest.fixture(scope="session")
def client(provider):
    """Client view bound to the tested backend."""
    return provider.get_client(RESOURCE, BACKEND_NAME or _single_backend(provider))


def _single_backend(provider) -> str:
    names = provider._get_client(RESOURCE, **CLIENT_ARGS).backend_names
    if len(names) != 1:
        pytest.skip(f"QHEAPPE_BACKEND not set and resource has backends {names}")
    return names[0]


# ---------- circuits ----------
# QuantumCircuit(n) + measure_all() gives a single 'meas' register, so counts keys
# are plain bitstrings ("00"), not "00 00" as with QuantumCircuit(n, n) + measure_all().


@pytest.fixture
def bell_circuit():
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    return qc


@pytest.fixture
def ghz_circuit():
    qc = QuantumCircuit(3)
    qc.h(0)
    qc.cx(0, 1)
    qc.cx(0, 2)
    qc.measure_all()
    return qc
