from __future__ import annotations

import asyncio
import importlib.util
from typing import Any
from uuid import UUID

from .backend_metadata import LexisResource, QBackendMetadata
from .qclient import QClient
from .qiskit.qbackend import QBackend
from .qiskit.qbackend_iqm import QBackendIQM

# Backwards compatibility: these names used to be local dataclasses of this module.
LexisResourceMetadata = LexisResource
__all__ = [
    "LexisResourceMetadata",
    "QBackend",
    "QBackendIQM",
    "QBackendMetadata",
    "QProvider",
    "QPulla",
]

DEFAULT_LEXIS_API_URL = "https://api.lexis.tech/"
IQM_SW_STACK = "iqm"  # value of "type" in /heappe/QScheduler/MachineInfo


def is_iqm_pulla_installed() -> bool:
    """Checks whether iqm-pulla is available in the current environment."""
    return (
        importlib.util.find_spec("iqm.pulla") is not None
        or importlib.util.find_spec("pulla") is not None
    )


if is_iqm_pulla_installed():
    from qheappe_client.qpulla import QPulla


def _split_target(
    lexis_resource: str | QBackendMetadata | None, backend_name: str | None
) -> tuple[str | None, str | None]:
    """Normalize (resource, backend) from either a resource name or a QBackendMetadata."""
    if isinstance(lexis_resource, QBackendMetadata):
        if backend_name is not None and backend_name != lexis_resource.backend_name:
            raise ValueError(
                f"backend_name '{backend_name}' conflicts with metadata of "
                f"'{lexis_resource.backend_name}'"
            )
        return lexis_resource.lexis_resource.resource_name, lexis_resource.backend_name
    return lexis_resource, backend_name


def _require_iqm(backend_metadata: QBackendMetadata) -> None:
    if (backend_metadata.software_stack or "").lower() != IQM_SW_STACK:
        raise NotImplementedError(
            f"Software stack '{backend_metadata.software_stack}' of backend "
            f"'{backend_metadata.backend_name}' is not supported."
        )
    if backend_metadata.dynamic_architecture is None:
        raise ValueError(
            f"Backend '{backend_metadata.backend_name}' has no dynamic architecture."
        )
    if backend_metadata.static_architecture is None:
        raise ValueError(
            f"Backend '{backend_metadata.backend_name}' has no static architecture."
        )


class QProvider:
    """QaaS wrapper around IQMProvider with LEXIS token authentication.

    One QClient is created and cached per LEXIS resource; it authenticates once and
    loads machine info for all clusters of that resource. Every backend handed out
    gets its own client view (QClient.bind), pinned to its cluster.
    """

    def __init__(
        self,
        lexis_project: str,
        token: str | None = None,
        provider_access_token=None,
        lexis_api_url: str = DEFAULT_LEXIS_API_URL,
    ):
        self._lexis_project = lexis_project
        self._token = token
        self._provider_token = provider_access_token
        self._lexis_api_url = lexis_api_url
        self._clients: dict[str | None, QClient] = {}

        self._event_loop = asyncio.new_event_loop()

        if self._token is None:
            from py4lexis.session import LexisSession

            self._session = LexisSession()
            self._token = self._session.get_access_token()
        else:
            self._session = None

    # -----------------------------------------------------------------
    # Client handling
    # -----------------------------------------------------------------

    def _get_client(
        self, lexis_resource_name: str | None, **client_kwargs: Any
    ) -> QClient:
        """Cached QClient per resource. Clients built with extra kwargs are not cached."""
        if not client_kwargs:
            cached = self._clients.get(lexis_resource_name)
            if cached is not None and cached.is_authenticated:
                return cached

        client = QClient(
            self._token,
            self._lexis_project,
            lexis_resource_name,
            self._provider_token,
            lexis_api_url=self._lexis_api_url,
            **client_kwargs,
        )
        client.connect()  # authenticate and load machine info

        if not client_kwargs:
            # None (auto-selected resource) and the explicit name share one client
            self._clients[lexis_resource_name] = client
            self._clients[client.lexis_resource.resource_name] = client
        return client

    def clear_cache(self) -> None:
        """Drop cached clients, e.g. after a token refresh."""
        self._clients.clear()

    # -----------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------

    @classmethod
    def list_available_backends(
        cls,
        token: str,
        lexis_project: str,
        provider_access_token=None,
        lexis_resource: str | None = None,
        lexis_api_url: str = DEFAULT_LEXIS_API_URL,
    ) -> dict[str, QBackendMetadata]:
        client = QClient(
            token,
            lexis_project,
            lexis_resource,
            provider_token=provider_access_token,
            lexis_api_url=lexis_api_url,
        )
        return client.event_loop.run_until_complete(client.get_available_backends())

    def available_backends(
        self, lexis_resource: str | None = None, **client_kwargs: Any
    ) -> dict[str, QBackendMetadata]:
        """All clusters of the resource, including unavailable ones (see .available / .error)."""
        return self._event_loop.run_until_complete(
            self._get_client(lexis_resource, **client_kwargs).get_available_backends()
        )

    def get_backend_info(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        **client_kwargs: Any,
    ) -> QBackendMetadata:
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs)
        return self._event_loop.run_until_complete(
            client.get_quantum_backend_info(
                backend_name=name, calibration_set_id=calibration_set_id
            )
        )

    # -----------------------------------------------------------------
    # Backends / Pulla / raw client
    # -----------------------------------------------------------------

    def get_backend(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        *,
        use_metrics: bool = False,
        **client_kwargs: Any,
    ) -> QBackend:
        """Factory for a QBackend instance, bound to the given resource and backend name."""
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs).bind(name)
        backend_metadata = client.get_quantum_backend_info(
            backend_name=name, calibration_set_id=calibration_set_id
        )
        _require_iqm(backend_metadata)

        if backend_metadata.software_stack.lower() != IQM_SW_STACK:
            raise NotImplementedError(
                f"Software stack '{backend_metadata.software_stack}' of backend "
                f"'{backend_metadata.backend_name}' is not supported."
            )

        return QBackendIQM(
            client=client,
            architecture=backend_metadata.dynamic_architecture,
            use_metrics=use_metrics,
            backend_metadata=backend_metadata,
            calibration_set_id=calibration_set_id,
        )

    def get_pulla(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        **client_kwargs: Any,
    ) -> QPulla:
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs).bind(name)
        backend_metadata = client.get_quantum_backend_info(
            backend_name=name, calibration_set_id=calibration_set_id
        )
        _require_iqm(backend_metadata)

        if not is_iqm_pulla_installed():
            raise ImportError(
                "The backend software stack is 'IQM', but the optional dependency "
                "'iqm-pulla' is not installed in the environment."
            )

        from qheappe_client.qpulla import QPulla

        # client is pinned to the backend, so get_pulla() targets the right cluster
        pulla_data, pulla = client.get_pulla()
        qbackend = QBackend(client, backend_metadata=backend_metadata)
        return QPulla(client, pulla, qbackend, **pulla_data)

    def get_client(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        **client_kwargs: Any,
    ) -> QClient:
        """Shared client of the resource, or a view pinned to backend_name if given."""
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs)
        return client.bind(name) if name is not None else client
