"""QaaS provider wrapper for IQM backends with LEXIS token authentication.

This module provides a high-level interface to interact with Lexis-backed quantum computing resources,
specifically focusing on IQM hardware. It handles client management, backend discovery,
and backend access through QBackend instances.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .backend_metadata import LexisResource, QBackendMetadata
from .iqm_compat import IQMStack, QBackendIQMFactory
from .iqm_compat.stack import install_hint
from .qclient import QClient
from .qiskit.qbackend import QBackend
from .qiskit.qbackend_iqm import (
    QBackendIQM,  # noqa: F401  # backwards-compatible re-export
)

# Backwards compatibility: these names used to be local dataclasses of this module.
LexisResourceMetadata = LexisResource

# Default URL for Lexis API requests
DEFAULT_LEXIS_API_URL = "https://api.lexis.tech/"
# IQM-specific software stack identifier
IQM_SW_STACK = "iqm"  # value of "type" in /heappe/QScheduler/MachineInfo


if TYPE_CHECKING:
    from .qpulla import QPulla


def is_iqm_pulla_installed() -> bool:
    """Check if IQM's pulse-level compiler (iqm-pulla) is installed.

    Returns:
        bool: True if ``iqm.cpc`` (iqm-pulla 15+) or ``iqm.pulla`` (older) is importable.
    """
    try:
        return (
            importlib.util.find_spec("iqm.cpc") is not None
            or importlib.util.find_spec("iqm.pulla") is not None
        )
    except ModuleNotFoundError:  # no 'iqm' namespace package at all
        return False


def _split_target(
    lexis_resource: str | QBackendMetadata | None, backend_name: str | None
) -> tuple[str | None, str | None]:
    """Normalize resource and backend name inputs.

    Args:
        lexis_resource: Either a string resource name or QBackendMetadata object.
        backend_name: Optional backend name to specify if not using metadata.

    Returns:
        tuple: (resource_name, backend_name) where resource_name is either the input
               resource or its metadata's resource_name.

    Raises:
        ValueError: If backend_name conflicts with metadata's backend_name.
    """
    if isinstance(lexis_resource, QBackendMetadata):
        if backend_name is not None and backend_name != lexis_resource.backend_name:
            raise ValueError(
                f"backend_name '{backend_name}' conflicts with metadata of "
                f"'{lexis_resource.backend_name}'"
            )
        return lexis_resource.lexis_resource.resource_name, lexis_resource.backend_name
    return lexis_resource, backend_name


def _require_iqm(backend_metadata: QBackendMetadata) -> None:
    """Validate IQM-specific requirements for a backend.

    Args:
        backend_metadata: Metadata object describing the backend to validate.

    Raises:
        NotImplementedError: If software stack is not 'iqm'.
        ValueError: If dynamic or static architecture is missing.
    """
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
    """QaaS wrapper for IQM backends with LEXIS token authentication.

    Manages client connections and provides access to quantum computing resources.
    Each resource gets its own cached client that authenticates once and maintains
    machine information for all clusters of that resource. Backend instances are
    created by binding clients to specific backend names.

    Attributes:
        _lexis_project: The Lexis project identifier.
        _token: Authentication token (auto-generated if not provided).
        _provider_token: Optional provider access token.
        _lexis_api_url: Base URL for Lexis API requests.
        _clients: Dictionary mapping resource names to QClient instances.
    """

    def __init__(
        self,
        lexis_project: str,
        token: str | None = None,
        provider_access_token=None,
        lexis_api_url: str = DEFAULT_LEXIS_API_URL,
    ):
        """Initialize the QProvider with Lexis credentials.

        Args:
            lexis_project: The Lexis project identifier.
            token: Optional authentication token. If not provided, will be auto-generated.
            provider_access_token: Optional provider access token.
            lexis_api_url: Base URL for Lexis API requests (default: DEFAULT_LEXIS_API_URL).
        """
        self._lexis_project = lexis_project
        self._token = token
        self._provider_token = provider_access_token
        self._lexis_api_url = lexis_api_url
        self._clients: dict[str | None, QClient] = {}

        # Create a new event loop for async operations
        self._event_loop = asyncio.new_event_loop()

        # Auto-generate token if not provided
        if self._token is None:
            from py4lexis.session import LexisSession

            self._session = LexisSession()
            self._token = self._session.get_access_token()
        else:
            self._session = None

    def _get_client(
        self, lexis_resource_name: str | None, **client_kwargs: Any
    ) -> QClient:
        """Get or create a cached QClient for the specified resource.

        Args:
            lexis_resource_name: The Lexis resource name (None for auto-selection).
            client_kwargs: Additional keyword arguments to pass to QClient constructor.

        Returns:
            QClient: Authenticated client instance bound to the resource.

        Notes:
            - Clients are cached per resource and reused if no additional kwargs are provided.
            - New clients with extra kwargs aren't cached for better isolation.
        """
        if not client_kwargs:
            cached = self._clients.get(lexis_resource_name)
            if cached is not None and cached.is_authenticated:
                return cached

        # Create new client with authentication and machine info loading
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
            # None (auto-selected) and explicit name share one client
            self._clients[lexis_resource_name] = client
            self._clients[client.lexis_resource.resource_name] = client
        return client

    def clear_cache(self) -> None:
        """Clear all cached QClient instances.

        Useful after token refresh or when cleaning up resources.
        """
        self._clients.clear()

    @classmethod
    def list_available_backends(
        cls,
        token: str,
        lexis_project: str,
        provider_access_token=None,
        lexis_resource: str | None = None,
        lexis_api_url: str = DEFAULT_LEXIS_API_URL,
    ) -> dict[str, QBackendMetadata]:
        """List all available backends for a given resource.

        Args:
            token: Authentication token.
            lexis_project: Lexis project identifier.
            provider_access_token: Optional provider access token.
            lexis_resource: Optional specific resource to limit discovery.
            lexis_api_url: Base URL for Lexis API requests.

        Returns:
            dict: Dictionary mapping backend names to their metadata objects.
        """
        client = QClient(
            token,
            lexis_project,
            lexis_resource,
            provider_token=provider_access_token,
            lexis_api_url=lexis_api_url,
        )
        return client.get_available_backends()

    def available_backends(
        self, lexis_resource: str | None = None, **client_kwargs: Any
    ) -> dict[str, QBackendMetadata]:
        """Get all clusters of a resource including unavailable ones.

        Args:
            lexis_resource: The Lexis resource name.
            client_kwargs: Additional keyword arguments for client initialization.

        Returns:
            dict: Dictionary mapping backend names to their metadata objects.
        """
        return self._get_client(
            lexis_resource, **client_kwargs
        ).get_available_backends()

    def get_backend_info(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        **client_kwargs: Any,
    ) -> QBackendMetadata:
        """Retrieve detailed information about a specific backend.

        Args:
            lexis_resource: Either resource name or QBackendMetadata object.
            backend_name: Optional backend name to specify if not using metadata.
            calibration_set_id: Optional calibration set ID for specific backend state.
            client_kwargs: Additional keyword arguments for client initialization.

        Returns:
            QBackendMetadata: Metadata object describing the requested backend.
        """
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs)
        return client.get_quantum_backend_info(
            backend_name=name, calibration_set_id=calibration_set_id
        )

    def get_backend(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        *,
        use_metrics: bool = False,
        strict_iqm_version: bool = True,
        **client_kwargs: Any,
    ) -> QBackend:
        """Create a QBackend instance bound to the specified resource and backend.

        For IQM, the backend class is picked by :class:`QBackendIQMFactory` from the
        installed iqm-client and the station-control version the backend reports.

        Args:
            lexis_resource: Either resource name or QBackendMetadata object.
            backend_name: Optional backend name to specify if not using metadata.
            calibration_set_id: Optional calibration set ID for specific backend state.
            use_metrics: Whether to enable metrics collection (default: False).
            strict_iqm_version: Raise if the installed IQM libraries do not match the
                backend; if ``False``, only warn (default: True).
            client_kwargs: Additional keyword arguments for client initialization.

        Returns:
            QBackendIQM: Configured backend instance for quantum operations
            (``QBackendIQM35`` or ``QBackendIQM36``).

        Raises:
            NotImplementedError: If software stack is not 'iqm'.
            ValueError: For missing dynamic/static architecture requirements.
            QException: Installed IQM libraries do not match the backend.
        """
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs).bind(name)
        backend_metadata = client.get_quantum_backend_info(
            backend_name=name, calibration_set_id=calibration_set_id
        )
        _require_iqm(backend_metadata)

        return QBackendIQMFactory.create(
            client,
            backend_metadata,
            calibration_set_id,
            use_metrics=use_metrics,
            strict=strict_iqm_version,
        )

    def get_pulla(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        calibration_set_id: UUID | None = None,
        *,
        strict_iqm_version: bool = True,
        walltime_limit_secs: int = 3600,
        job_name: str = "qpulla_job",
        use_sessions: bool = False,
        **client_kwargs: Any,
    ) -> QPulla:
        """Get pulse-level access (IQM Pulla) to a backend.

        The returned :class:`~qheappe_client.qpulla.QPulla` is an
        :class:`iqm.iqm_client.IQMClient` connected through HEAppE: compile with
        ``get_standard_compiler()`` / ``qiskit_to_iqm()`` and run with
        ``submit_playlist()``, as in IQM's Pulla documentation. Its ``backend`` is the
        Qiskit backend of the same machine.

        Needs the IQM 36 stack (``qaas[iqm_qiskit]``), QScheduler v0.2.2+ with pulse
        access enabled for the machine, and HEAppE 6.7.0+.

        Args:
            lexis_resource: Either resource name or QBackendMetadata object.
            backend_name: Optional backend name to specify if not using metadata.
            calibration_set_id: Calibration set of the Qiskit backend (transpilation);
                current if ``None``.
            strict_iqm_version: Raise if the installed IQM libraries do not match the
                backend; if ``False``, only warn (default: True).
            walltime_limit_secs: HEAppE walltime limit of each submitted task.
            job_name: HEAppE job name of submitted jobs.
            use_sessions: Run submitted tasks in QScheduler sessions.
            client_kwargs: Additional keyword arguments for client initialization.

        Returns:
            QPulla: Pulse-level client bound to the backend.

        Raises:
            NotImplementedError: Software stack is not 'iqm', or the installed IQM stack
                is not the IQM 36 one.
            ValueError: Missing dynamic/static architecture.
            QException: Installed IQM libraries do not match the backend.
        """
        backend = self.get_backend(
            lexis_resource,
            backend_name,
            calibration_set_id,
            strict_iqm_version=strict_iqm_version,
            **client_kwargs,
        )
        if backend.iqm_stack is not IQMStack.IQM36:
            raise NotImplementedError(
                f"QPulla needs the IQM 36 stack (iqm-client 36.x, iqm-pulla 15.x); "
                f"{backend.iqm_stack} is installed. Install it with: "
                f"{install_hint(IQMStack.IQM36)}"
            )

        from .qpulla import QPulla

        pulla = QPulla(
            backend._qclient,
            backend,
            walltime_limit_secs=walltime_limit_secs,
            job_name=job_name,
            use_sessions=use_sessions,
        )
        backend.client = pulla
        return pulla

    def get_client(
        self,
        lexis_resource: str | QBackendMetadata | None = None,
        backend_name: str | None = None,
        **client_kwargs: Any,
    ) -> QClient:
        """Get a client bound to either the resource or specific backend.

        Args:
            lexis_resource: Either resource name or QBackendMetadata object.
            backend_name: Optional backend name to specify if not using metadata.
            client_kwargs: Additional keyword arguments for client initialization.

        Returns:
            QClient: Client instance bound to the specified target.
        """
        resource_name, name = _split_target(lexis_resource, backend_name)
        client = self._get_client(resource_name, **client_kwargs)
        return client.bind(name) if name is not None else client
