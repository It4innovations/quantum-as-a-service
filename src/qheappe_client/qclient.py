"""
Async QaaS client: LEXIS authentication and HEAppE QScheduler over :mod:`aiohttp`.

The client authenticates a LEXIS JWT, resolves the LEXIS quantum resource of a
project and all quantum clusters (assignments) behind it, and talks to the
HEAppE QScheduler REST API of each cluster (job submission, results, machine
info, sessions, job events).

Both an async API (``await`` / ``async with``) and a sync API are provided.
The sync API runs coroutines on a private event loop in a background thread
owned by the client (:class:`_LoopThread`), so it also works where the calling
thread already runs a loop (Jupyter, IPython, async frameworks).

The HTTP session is bound to the loop it was created on. Requests issued from
any other loop are transparently executed on the session's loop, so sync and
async calls can be mixed on one client. The only unsupported combination is a
*sync* call made from the very loop that owns the session (client connected
with the async API, then a sync method called from the same loop): that would
block the loop the request needs, so it raises :class:`QException`.

Async usage::

    async with await QClient.create(token, "my_project", "my_resource") as client:
        md = await client.get_backend("VLQ")
        job = await client.submit_quantum_job(tasks, circuit_payloads=payloads)
        state = await client.await_for_job_terminal_state(job["Id"], timeout=600)
        results = await client.aget_job_results(job, artifact_names=["measurement_counts"])

Sync usage::

    with QClient(token, "my_project", "my_resource") as client:
        info = client.get_quantum_backend_info("VLQ")

Environment variables:

* ``QPROVIDER_LOGLEVEL`` -- logger level (default ``INFO``).
* ``QPROVIDER_LOGFILE`` -- log to this file instead of stderr.
* ``QPROVIDER_LOG_BODIES`` -- ``1``/``true``/``yes`` adds response body previews
  to debug logs (may contain user or project data).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import copy
import itertools
import json
import logging
import os
import ssl
import sys
import threading
import time
import warnings
from collections.abc import AsyncIterator, Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from importlib import metadata as m
from typing import Any, Self
from uuid import UUID

import aiohttp
import jwt
import truststore
from py4heappe.heappe_v6.core import ApiClient as HEAppEApi
from py4heappe.heappe_v6.core import Configuration as HEAppEConfiguration
from py4heappe.heappe_v6.core.api import ClusterInformationApi, JobManagementApi
from py4heappe.heappe_v6.core.models import ClusterExt

from .backend_metadata import (
    LexisProject,
    LexisResource,
    QBackendMetadata,
    QCluster,
    QMachineInfo,
)
from .utils import (
    QAuthException,
    QException,
    QResultsFailed,
    from_swagger_json,
    normalize_query,
    req_body_preview,
    to_utc,
)

# Module logger: level and target (file / stderr) configured via environment.
log = logging.getLoggerClass()(
    __name__, os.environ.get("QPROVIDER_LOGLEVEL", "INFO").upper()
)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logfile = os.environ.get("QPROVIDER_LOGFILE", None)
handler = (
    logging.FileHandler(logfile, mode="a")
    if logfile
    else logging.StreamHandler(sys.stderr)
)
handler.setFormatter(formatter)
log.addHandler(handler)

# Opt-in response body previews in debug logs (may contain user/project data).
_LOG_BODIES = os.environ.get("QPROVIDER_LOG_BODIES", "").lower() in ("1", "true", "yes")

# Process-wide request counter; correlates request/response lines in debug logs.
_request_ids = itertools.count(1)


# Only asymmetric algorithms are accepted from the token header (prevents alg confusion).
_ALLOWED_JWT_ALGS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class _LoopThread:
    """
    Event loop running forever in a daemon thread; backs the sync API.

    Coroutines are submitted with :func:`asyncio.run_coroutine_threadsafe` and
    the caller blocks on the resulting :class:`concurrent.futures.Future`. The
    calling thread never touches its own event loop, so this works from plain
    scripts as well as from threads that already run a loop (Jupyter).

    Attributes:
        POLL_INTERVAL (float): Wait slice in seconds; keeps the blocked caller
            responsive to ``KeyboardInterrupt`` on every platform.
    """

    POLL_INTERVAL = 0.2

    def __init__(self, name: str = "qclient-loop"):
        self.loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self.thread = threading.Thread(target=self._main, name=name, daemon=True)
        self.thread.start()
        self._started.wait()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._started.set()
        try:
            self.loop.run_forever()
        finally:
            # Same teardown as asyncio.run(): cancel leftovers, finalize async generators.
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
            finally:
                self.loop.close()

    @property
    def alive(self) -> bool:
        """Whether the loop accepts work."""
        return self.thread.is_alive() and not self.loop.is_closed()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """
        Run ``coro`` on the loop thread and block until it completes.

        On ``KeyboardInterrupt`` (or any other interruption of the wait) the
        coroutine is cancelled on the loop thread before the exception
        propagates, so in-flight requests are aborted rather than orphaned.

        Args:
            coro: Coroutine to run.

        Returns:
            The coroutine's result.

        Raises:
            QException: Called from the loop thread itself (would deadlock), or
                the loop is already stopped.
        """
        if threading.get_ident() == self.thread.ident:
            coro.close()
            raise QException(
                "Sync QClient API called from the client's own event loop thread "
                "(would deadlock); use the async methods with 'await'."
            )
        if not self.alive:
            coro.close()
            raise QException("QClient event loop is closed; the client was closed.")
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as e:  # loop closed between the check and the submit
            coro.close()
            raise QException(f"QClient event loop is closed: {e!s}") from None
        try:
            while not fut.done():
                concurrent.futures.wait([fut], timeout=self.POLL_INTERVAL)
            return fut.result()
        except BaseException:
            fut.cancel()  # no-op when already done
            raise

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the loop and join the thread. Safe to call more than once."""
        if self.thread.is_alive():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:  # loop already closed
                pass
            self.thread.join(timeout)


@dataclass(frozen=True)
class HttpResult:
    """
    Fully read HTTP response.

    Attributes:
        status (int): HTTP status code.
        content_type (str): Response ``Content-Type`` (without parameters).
        body (bytes): Raw response body.
    """

    status: int
    content_type: str
    body: bytes

    def text(self) -> str:
        """
        Body decoded as UTF-8 (invalid bytes replaced).

        Returns:
            str
        """
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """
        Body decoded as JSON.

        Returns:
            Any

        Raises:
            ValueError: Body is not valid JSON.
        """
        return json.loads(self.body)

    def parsed(self) -> Any:
        """
        Body decoded as JSON if possible, raw bytes otherwise.

        Returns:
            Any or bytes
        """
        try:
            return self.json()
        except ValueError:
            return self.body


class MachineCalibrationIQMEndpoint(str, Enum):
    """
    IQM calibration endpoints exposed via ``/heappe/QScheduler/MachineCalibration``.

    Attributes:
        METRICS: Calibration quality metrics (fidelities, coherence times, ...).
        DYNAMIC_QUANTUM_ARCHITECTURE: Qubits, couplers and gates calibrated in
            a calibration set.
    """

    METRICS = "metrics"
    DYNAMIC_QUANTUM_ARCHITECTURE = "dynamic-quantum-architecture"


class QClient:
    """
    Async client for quantum backends behind LEXIS and HEAppE QScheduler.

    One client is bound to one LEXIS resource, which can contain several quantum
    clusters (LEXIS assignments, called *backends* here). During :meth:`_connect`
    the client:

    #. verifies the JWT and LEXIS project membership;
    #. resolves the quantum resource and its clusters;
    #. resolves HEAppE cluster, node type and project IDs (concurrently per
       HEAppE instance);
    #. optionally loads machine info of all clusters (concurrently).

    Failures in steps 3 and 4 do not abort the connection; they mark the
    affected cluster unavailable (``available=False``, ``error=...``).

    The working backend is selected with :meth:`get_backend` (a single-cluster
    resource is selected implicitly). Every job, session and machine-info method
    also accepts an explicit ``backend_name``; :meth:`bind` returns a view pinned
    to one backend.

    Construction:

    * ``await QClient.create(...)`` -- construct and connect;
    * ``async with QClient(...) as client:`` -- connect on enter, close on exit;
    * ``with QClient(...) as client:`` -- same, with the sync API.

    Args:
        token (str): LEXIS JWT access token, also sent as ``Bearer`` to HEAppE.
        lexis_project (str): LEXIS project short name.
        lexis_resource_name (str or None): LEXIS resource name; required only if the project
            has more than one quantum resource.
        provider_token (str or None): Additional provider-specific token (stored, currently unused).
        lexis_api_url (str or None): LEXIS Platform API base URL; ``/userorg`` is appended.
        load_machine_info (bool): Load machine info of all clusters on connect;
            when ``False`` it is loaded lazily on first use of a backend.
        session (aiohttp.ClientSession or None): Externally managed :class:`aiohttp.ClientSession`; never closed
            by the client.
        kwargs: Reserved for future options (stored, unused).

    Attributes:
        DEFAULT_USERORG_BASE_URL (str): Default LEXIS Platform API base URL.
        DEFAULT_QUANTUM_LOCATION_TYPE (int): LEXIS ``LocationTypeId`` of quantum locations.
        SUPPORTED_SW_STACKS (tuple[str, ...]): Backend software stacks this client can drive.
        TERMINAL_JOB_STATES (tuple[str, ...]): Upper-case job states a job never leaves (both
            spellings of *cancelled* are accepted).
        DEFAULT_TIMEOUT (float): Default total timeout of one HTTP request, in seconds.
    """

    DEFAULT_USERORG_BASE_URL = "https://api.lexis.tech/"
    DEFAULT_QUANTUM_LOCATION_TYPE = 7
    SUPPORTED_SW_STACKS = ("iqm",)
    TERMINAL_JOB_STATES = ("FINISHED", "FAILED", "CANCELLED", "CANCELED")
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        token: str,
        lexis_project: str,
        lexis_resource_name: str | None = None,
        provider_token: str | None = None,
        lexis_api_url: str | None = None,
        load_machine_info: bool = True,
        session: aiohttp.ClientSession | None = None,
        **kwargs,
    ):
        """
        Store configuration only; no I/O is performed.

        Call ``await create(...)``, use ``async with`` / ``with``, or call
        :meth:`connect` before using the client. Parameters are documented on
        the class.
        """
        self._token = token
        self._lexis_project = lexis_project
        self._lexis_resource_name = lexis_resource_name
        self.provider_token = provider_token
        # Background loop thread for the sync API; created on first sync call.
        self._loop_thread: _LoopThread | None = None
        self._load_machine_info_on_connect = load_machine_info
        self._kwargs = kwargs

        # Filled by _connect().
        self._authenticated = False
        self._username: str | None = None
        self._lexis_project_info: dict[str, Any] = {}
        self._lexis_project_obj: LexisProject | None = None
        self._lexis_resource: LexisResource | None = None
        self._active_cluster: str | None = None

        # HTTP session: injected sessions are never closed by the client
        self._session = session
        self._owns_session = session is None
        # Loop the session is bound to; requests from other loops are forwarded there.
        # (aiohttp stores it as ClientSession._loop; None -> no forwarding.)
        self._session_loop: asyncio.AbstractEventLoop | None = getattr(
            session, "_loop", None
        )
        self._is_view = False  # True for clients returned by bind()

        # HEAppE ProjectId cache per HEAppE URL (fallback lookup via accounting string)
        self._heappe_project_ids: dict[str, int] = {}
        # Keyed by (url, loop): an asyncio.Lock binds to the loop that first waits on it.
        self._project_locks: dict[tuple[str, asyncio.AbstractEventLoop], asyncio.Lock] = {}
        # Lazily created sync py4heappe clients, only for callers needing the generated API
        self._heappe_clients: dict[str, HEAppEApi] = {}

        base = lexis_api_url or self.DEFAULT_USERORG_BASE_URL
        if lexis_api_url and lexis_api_url != self.DEFAULT_USERORG_BASE_URL:
            log.info("Using %s LEXIS Platform API", lexis_api_url)
        self._lexis_userorg_api_url = f"{base.rstrip('/')}/userorg"

    # =================================================================
    # Lifecycle
    # =================================================================

    @classmethod
    async def create(cls, *args: Any, **kwargs: Any) -> QClient:
        """
        Construct and connect a client in one step.

        Arguments are passed to :meth:`__init__`. On failure the client is closed
        before the exception propagates.

        Returns:
            QClient: Connected client.

        Raises:
            QAuthException: Authentication or resource authorization failed.
            QException: Connection failed.
        """
        client = cls(*args, **kwargs)
        try:
            await client._connect()
        except BaseException:
            await client.aclose()
            raise
        return client

    def _check_sync_allowed(self, coro) -> None:
        """
        Reject a sync call that would block the loop owning the HTTP session.

        If the caller runs on the session's loop (client connected with the
        async API), blocking here would stop that loop from serving the very
        requests we wait for.

        Raises:
            QException: Caller runs on the session's loop.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return  # plain sync caller
        if running is not None and running is self._session_loop:
            coro.close()  # avoid "coroutine was never awaited" warning
            raise QException(
                "Sync QClient API called from the event loop that owns this client's "
                "HTTP session (client connected with the async API); use 'await' with "
                "the async methods instead."
            )

    def _run(self, coro):
        """
        Run a coroutine on the client's background loop thread (sync API).

        The :class:`_LoopThread` is created on first use and stopped by
        :meth:`close`, which is also registered with :mod:`atexit`. Works from
        threads with a running loop (e.g. Jupyter): the caller only blocks on a
        future, it does not re-enter its own loop.

        Args:
            coro: Coroutine to run.

        Returns:
            The coroutine's result.

        Raises:
            QException: Called from the loop owning the HTTP session, from the
                client's own loop thread, or after :meth:`close`.
        """
        self._check_sync_allowed(coro)
        if self._loop_thread is None:
            if self._is_view:
                coro.close()
                raise QException("QClient view is closed")
            self._loop_thread = _LoopThread()
            # Stop the loop thread and close the session when the program exits.
            atexit.register(self.close)
        return self._loop_thread.run(coro)

    async def _on_session_loop(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """
        Await ``coro`` on the loop the HTTP session is bound to.

        Runs it in place when the current loop owns the session (or the owner
        is unknown); otherwise forwards it with
        :func:`asyncio.run_coroutine_threadsafe` and awaits the result without
        blocking the current loop. Cancellation is propagated to the forwarded
        coroutine.

        Args:
            coro: Coroutine to run.

        Returns:
            The coroutine's result.

        Raises:
            QException: The session's loop is closed or no longer running.
        """
        owner = self._session_loop
        if owner is None or owner is asyncio.get_running_loop():
            return await coro
        if owner.is_closed() or not owner.is_running():
            coro.close()
            raise QException(
                "HTTP session is bound to an event loop that is no longer running; "
                "close and reconnect the client."
            )
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, owner))

    async def _connect(self) -> QClient:
        """
        Authenticate against LEXIS, discover clusters and load machine info.

        Idempotent: returns immediately when already authenticated. Creates an
        owned HTTP session if none is open.

        Returns:
            QClient: This client.

        Raises:
            QAuthException: Token, project or resource authorization failed.
            QException: HTTP session or request failure.
        """
        if self._authenticated:
            return self
        # Create a new session if none exists or if the existing one is closed.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl_context()),
                timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT),
            )
            self._owns_session = True
            self._session_loop = asyncio.get_running_loop()

        started = time.perf_counter()
        log.debug(
            "Connecting: project=%s resource=%s api=%s",
            self._lexis_project,
            self._lexis_resource_name or "<auto>",
            self._lexis_userorg_api_url,
        )
        (
            self._username,
            self._lexis_project_info,
        ) = await self._authenticate_authorize_lexis()
        self._authenticated = True
        self._lexis_project_obj = LexisProject(
            self._lexis_project,
            self._lexis_project_info.get("StartDate") or "-",
            self._lexis_project_info.get("EndDate") or "-",
        )
        self._lexis_resource = await self._authorize_lexis_resource(
            self._lexis_resource_name
        )

        # Per-cluster failures below are recorded on the cluster, not raised.
        await self._resolve_heappe_clusters()
        if self._load_machine_info_on_connect:
            await self._load_machine_info_all()

        # Convenience: a single-cluster resource is selected implicitly
        if len(self._lexis_resource.clusters) == 1:
            self._active_cluster = next(iter(self._lexis_resource.clusters))
        log.debug(
            "Connected in %.0fms: user=%s resource=%s backends=%s active=%s",
            (time.perf_counter() - started) * 1000,
            self._username,
            self._lexis_resource.resource_name,
            {n: c.available for n, c in sorted(self._lexis_resource.clusters.items())},
            self._active_cluster,
        )
        return self

    def connect(self) -> QClient:
        """
        Sync wrapper of :meth:`_connect`.

        On failure the client is closed so the HTTP session and loop do not leak.

        Returns:
            QClient: This client.

        Raises:
            QAuthException: Authentication or authorization failed.
            QException: Connection failed, or called from the loop owning the HTTP session.
        """
        try:
            return self._run(self._connect())
        except BaseException:
            self.close()  # don't leak the session on auth/connect errors
            raise

    async def aclose(self) -> None:
        """
        Close the owned HTTP session and clear authentication state.

        Single place for cleanup logic: ``async with`` calls it from
        :meth:`__aexit__`, and :meth:`close` runs it on the client's loop.

        On a view returned by :meth:`bind` only the view is deactivated; the shared
        session stays open for the parent and other views. Injected sessions are
        never closed.
        """
        self._authenticated = False
        self._active_cluster = None
        if self._is_view:
            return

        # Rebind instead of .clear(): views share these dicts by reference (copy.copy)
        self._heappe_clients = {}
        self._heappe_project_ids = {}

        if (
            self._session is not None
            and self._owns_session
            and not self._session.closed
        ):
            log.debug("Closing HTTP session")
            try:
                # The session must be closed on the loop it was created on.
                await self._on_session_loop(self._session.close())
            except QException as e:
                log.warning("HTTP session not closed cleanly: %s", e)
        self._session = None
        self._session_loop = None

    def close(self) -> None:
        """
        Sync wrapper of :meth:`aclose`; the owning client also stops its loop thread.

        Safe to call more than once. A no-op if the sync API was never used
        (async callers use ``await aclose()``). A :meth:`bind` view never stops
        the shared loop thread.

        Raises:
            QException: Called from the loop that owns the HTTP session (use
                ``await aclose()`` there).
        """
        loop_thread = self._loop_thread
        if loop_thread is None:
            return  # sync API never used; async callers use `await aclose()`

        if self._is_view:
            self._loop_thread = None
            try:
                loop_thread.run(self.aclose())
            except QException:  # parent already stopped the shared loop
                pass
            return

        coro = self.aclose()
        self._check_sync_allowed(coro)
        atexit.unregister(self.close)
        self._loop_thread = None
        try:
            loop_thread.run(coro)
        finally:
            loop_thread.stop()

    # Backwards-compatible name
    close_session = close

    async def __aenter__(self) -> Self:
        """
        Connect on ``async with`` entry; close on failure.

        Returns:
            QClient
        """
        try:
            return await self._connect()
        except BaseException:
            await self.aclose()
            raise

    def __enter__(self) -> Self:
        """
        Connect on ``with`` entry (sync API).

        Returns:
            QClient
        """
        return self.connect()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Call :meth:`aclose`. Exceptions are not suppressed."""
        await self.aclose()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Call :meth:`close`. Exceptions are not suppressed."""
        self.close()

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        """
        TLS context for all connections.

        Uses the OS trust store on Windows (via :mod:`truststore`), the default
        OpenSSL context elsewhere.

        Returns:
            ssl.SSLContext
        """
        if sys.platform == "win32":
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return ssl.create_default_context()

    # =================================================================
    # Low-level HTTP
    # =================================================================

    def _require_session(self) -> aiohttp.ClientSession:
        """
        Return the open HTTP session.

        Returns:
            aiohttp.ClientSession

        Raises:
            QException: No session is open.
        """
        if self._session is None or self._session.closed:
            raise QException("HTTP session is not open; call connect() first")
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        """
        ``Authorization: Bearer <token>`` header used for LEXIS and HEAppE.

        Returns:
            dict[str, str]
        """
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResult:
        """
        Perform one authenticated HTTP request and read the whole body.

        The body is read inside the response context, so the connection is
        released before returning.

        Args:
            method (str): HTTP method.
            url (str): Absolute URL.
            params (dict[str, Any] or None): Query parameters, normalized by :func:`normalize_query`.
            json_body (Any): JSON body.
            data (Any): Raw body, e.g. :class:`aiohttp.MultipartWriter`.
            timeout (float or None): Total timeout in seconds; :attr:`DEFAULT_TIMEOUT` if ``None``.

        Returns:
            HttpResult: Status, content type and body.

        Raises:
            QException: HTTP status >= 400 (first 500 bytes of the body included),
                connection error or timeout.

        Note:
            Executed on the loop that owns the HTTP session (see
            :meth:`_on_session_loop`), so it may be awaited from any loop.
        """
        return await self._on_session_loop(
            self._request_impl(
                method,
                url,
                params=params,
                json_body=json_body,
                data=data,
                timeout=timeout,
            )
        )

    async def _request_impl(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResult:
        """Body of :meth:`_request`; must run on the session's loop."""
        session = self._require_session()
        query = normalize_query(params)
        rid = next(_request_ids)
        started = time.perf_counter()
        try:
            async with session.request(
                method,
                url,
                params=query,
                json=json_body,
                data=data,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout or self.DEFAULT_TIMEOUT),
            ) as resp:
                body = await resp.read()
                if log.isEnabledFor(logging.DEBUG):
                    # Body preview only on opt-in or on error responses.
                    log.debug(
                        "[#%d] <- %s %s %d %s %dB %.0fms%s",
                        rid,
                        method,
                        resp.url.path,
                        resp.status,
                        resp.content_type,
                        len(body),
                        (time.perf_counter() - started) * 1000,
                        f" body={req_body_preview(body)}"
                        if _LOG_BODIES or resp.status >= 400
                        else "",
                    )
                if resp.status >= 400:
                    detail = body[:500].decode("utf-8", errors="replace")
                    raise QException(f"{method} {url} -> HTTP {resp.status}: {detail}")
                return HttpResult(resp.status, resp.content_type, body)
        except QException:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            log.debug(
                "[#%d] x  %s %s failed after %.0fms: %s: %s",
                rid,
                method,
                url,
                (time.perf_counter() - started) * 1000,
                type(e).__name__,
                e,
            )
            raise QException(f"{method} {url} failed: {type(e).__name__}: {e!s}") from e

    @staticmethod
    def _join(base: str, path: str) -> str:
        """
        Join a base URL and an absolute path without doubling the slash.

        Args:
            base (str): Base URL, with or without trailing slash.
            path (str): Path starting with ``/``.

        Returns:
            str
        """
        return f"{base.rstrip('/')}{path}"

    def _heappe_url(self, path: str, backend_name: str | None = None) -> str:
        """
        Absolute HEAppE URL of ``path`` on the given (or active) backend.

        Args:
            path (str): HEAppE API path, e.g. ``/heappe/JobManagement/CancelJob``.
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            str

        Raises:
            QException: Backend unknown or none selected.
        """
        return self._join(self._resolve_cluster(backend_name).heappe_url, path)

    async def _heappe_get(
        self,
        cluster: QCluster,
        path: str,
        params: dict[str, Any] | None = None,
        **kw: Any,
    ) -> HttpResult:
        """
        ``GET`` on the cluster's HEAppE instance.

        Args:
            cluster (QCluster): Target cluster.
            path (str): HEAppE API path.
            params (dict[str, Any] or None): Query parameters.
            kw: Extra arguments for :meth:`_request` (e.g. ``timeout``).

        Returns:
            HttpResult

        Raises:
            QException: Request failed.
        """
        return await self._request(
            "GET", self._join(cluster.heappe_url, path), params=params, **kw
        )

    # =================================================================
    # Authentication
    # =================================================================

    def _verify_backend_version(self, cluster: QCluster):
        """
        Check the installed ``qaas`` package against the cluster's QScheduler version.

        Versions are compared as semantic versions (leading ``v`` ignored):

        * major or minor mismatch -- raises :class:`QException`;
        * patch mismatch -- emits a :class:`UserWarning`.

        Args:
            cluster (QCluster): Cluster with loaded ``machine_info``.

        Raises:
            QException: Major or minor version differs.
            ValueError: A version is not ``X.Y.Z``.
        """

        def _parse_semver(v: str):
            v = (v or "").strip()
            if v.startswith(("v", "V")):
                v = v[1:]
            parts = v.split(".")
            if len(parts) < 3:
                raise ValueError(f"Invalid semantic version: {v!r}")
            # Pre-release/build suffixes beyond the third component are ignored.
            return tuple(int(p) for p in parts[:3])

        client_version = m.version("qaas")
        server_version = cluster.machine_info.qscheduler_version
        exp_major, exp_minor, exp_patch = _parse_semver(client_version)
        act_major, act_minor, act_patch = _parse_semver(server_version)

        if act_major != exp_major or act_minor != exp_minor:
            raise QException(
                f"Major or Minor Version Mismatch on backend '{cluster.name}': "
                f"client must be updated. (expected={client_version}, actual={server_version})."
            )
        if act_patch != exp_patch:
            warnings.warn(
                f"Patch Version Mismatch on backend '{cluster.name}': recommending update "
                f"(expected={client_version}, actual={server_version}).",
                category=UserWarning,
                stacklevel=2,
            )

    async def _verify_token(self) -> str:
        """
        Verify the JWT and return the user identifier.

        Steps:

        #. read ``iss`` and the header ``alg`` / ``kid`` without verification;
        #. reject algorithms outside :data:`_ALLOWED_JWT_ALGS`;
        #. fetch the issuer's OpenID configuration and JWKS (via :mod:`aiohttp`);
        #. verify signature, issuer and expiration with the key matching ``kid``
           (first key if the token has no ``kid``).

        The audience is not verified.

        Returns:
            str: ``preferred_username``, or ``sub`` if absent.

        Raises:
            ValueError: Missing ``iss``, unsupported ``alg``, missing
                ``jwks_uri`` or no matching key.
            jwt.InvalidTokenError: Signature, issuer or expiry check failed.
            QAuthException: Token has no user identifier or is expired.
            QException: OpenID configuration or JWKS could not be fetched.
        """
        unverified = jwt.decode(self._token, options={"verify_signature": False})
        issuer = unverified.get("iss")
        if not issuer:
            raise ValueError("JWT token missing issuer (iss) claim")
        log.debug("Detected JWT issuer: %s", issuer)

        header = jwt.get_unverified_header(self._token)
        alg = header.get("alg")
        if alg not in _ALLOWED_JWT_ALGS:
            raise ValueError(f"Unsupported JWT algorithm {alg!r}")

        log.debug(
            "Fetching OpenID configuration and JWKS (alg=%s, kid=%s)",
            alg,
            header.get("kid"),
        )
        oidc = (
            await self._request(
                "GET", f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            )
        ).json()
        jwks_url = oidc.get("jwks_uri")
        if not jwks_url:
            raise ValueError("OpenID configuration response is missing 'jwks_uri'")
        jwk_set = jwt.PyJWKSet.from_dict((await self._request("GET", jwks_url)).json())

        # Select the signing key by kid; any key if the token does not name one.
        kid = header.get("kid")
        keys = [k for k in jwk_set.keys if kid is None or k.key_id == kid]
        if not keys:
            raise ValueError(f"No signing key with kid={kid!r} in JWKS")

        decoded = jwt.decode(
            self._token,
            keys[0].key,
            algorithms=[alg],
            issuer=issuer,
            audience="portal",
            options={"verify_exp": True, "verify_iss": True, "verify_aud": False},
        )
        user_id = decoded.get("preferred_username") or decoded.get("sub")
        if not user_id:
            raise QAuthException(
                reason="Invalid token: missing user information",
                user_id=user_id,
                resource=self._lexis_project,
            )
        # Redundant with verify_exp; kept as an explicit guard.
        exp = decoded.get("exp")
        if exp and exp < time.time():
            raise QAuthException(
                reason="Token has expired",
                user_id=user_id,
                resource=self._lexis_project,
            )
        log.debug("JWT token validated successfully for user: %s", user_id)
        return user_id

    async def _authenticate_authorize_lexis(self) -> tuple[str, dict[str, Any]]:
        """
        Verify the JWT and the user's access to the LEXIS project.

        Checks token validity (:meth:`_verify_token`), project membership via
        UserOrg ``/api/Project``, and the project activity window
        (``StartDate`` / ``EndDate``) when both dates are present.

        Returns:
            tuple[str, dict[str, Any]]: ``(user_id, project_info)``; ``project_info`` is the raw UserOrg record.

        Raises:
            QAuthException: Any check failed. For expected token errors the
                cause chain is suppressed; unexpected errors keep it.
        """
        try:
            user_id = await self._verify_token()
        except QAuthException:
            raise
        except (jwt.InvalidTokenError, ValueError) as e:
            # Expected token problems: concise error, details in debug log only.
            log.debug("Token verification failed", exc_info=True)
            raise QAuthException.from_cause(e, resource=self._lexis_project) from None
        except Exception as e:
            # Unexpected (network, OIDC/JWKS fetch, ...): keep the cause chain
            raise QAuthException.from_cause(e, resource=self._lexis_project) from e

        log.debug("Fetching LEXIS project '%s'", self._lexis_project)
        try:
            projects_data = (
                await self._request(
                    "GET",
                    f"{self._lexis_userorg_api_url}/api/Project",
                    params={"ProjectShortName": self._lexis_project},
                    timeout=30,
                )
            ).json()
        except Exception as e:
            raise QAuthException.from_cause(
                e, user_id=user_id, resource=self._lexis_project
            ) from e

        # The filter param is not trusted alone: match the short name explicitly.
        project_info = next(
            (p for p in projects_data if p.get("ShortName") == self._lexis_project),
            None,
        )
        if project_info is None:
            available = sorted(
                {p.get("ShortName") for p in projects_data if p.get("ShortName")}
            )
            raise QAuthException(
                reason=f"User does not have access to LEXIS project '{self._lexis_project}'. "
                f"Available projects: {available}",
                user_id=user_id,
                resource=self._lexis_project,
            )
        log.debug("Project access verified: %s", self._lexis_project)

        start, end = project_info.get("StartDate"), project_info.get("EndDate")
        if start and end:
            try:
                start_dt, end_dt = to_utc(start), to_utc(end)
            except ValueError as date_error:
                log.warning("Could not parse project dates: %s", date_error)
                raise QAuthException("Could not parse project dates") from date_error
            now = datetime.now(UTC)
            if now < start_dt:
                raise QAuthException(
                    reason=f"Project '{self._lexis_project}' has not started yet. Start date: {start}",
                    user_id=user_id,
                    resource=self._lexis_project,
                )
            if now > end_dt:
                raise QAuthException(
                    reason=f"Project '{self._lexis_project}' has expired. End date: {end}",
                    user_id=user_id,
                    resource=self._lexis_project,
                )

        return user_id, project_info

    @staticmethod
    def _spec_value(assignment: dict[str, Any], key: str) -> str | None:
        """
        Value of a LEXIS assignment specification (``Specifications[].Key``).

        Args:
            assignment (dict[str, Any]): LEXIS assignment record.
            key (str): Specification key, e.g. ``"HEAPPE_URL"``.

        Returns:
            str or None: First matching value, or ``None``.
        """
        for spec in assignment.get("Specifications", []) or []:
            if spec.get("Key") == key:
                return spec.get("Value")
        return None

    def _is_quantum_assignment(self, assignment: dict[str, Any]) -> bool:
        """
        Whether the assignment points to a quantum location.

        Args:
            assignment (dict[str, Any]): LEXIS assignment record.

        Returns:
            bool
        """
        return assignment.get("LocationTypeId") == self.DEFAULT_QUANTUM_LOCATION_TYPE

    async def _authorize_lexis_resource(
        self, lexis_resource_name: str | None = None
    ) -> LexisResource:
        """
        Select the LEXIS quantum resource and collect its clusters.

        A resource is *quantum* if any of its assignments has
        ``LocationTypeId ==`` :attr:`DEFAULT_QUANTUM_LOCATION_TYPE`. Each quantum
        assignment with a ``LocationName`` and a ``HEAPPE_URL`` specification
        becomes one :class:`QCluster`; incomplete assignments are skipped with a
        warning and duplicate location names keep the first assignment.

        Args:
            lexis_resource_name (str or None): Resource name. If ``None``, the project must
                contain exactly one quantum resource.

        Returns:
            LexisResource: Resource with its clusters (HEAppE IDs not yet resolved).

        Raises:
            QAuthException: Not authenticated, request failed, no / ambiguous /
                unknown quantum resource, resource outside its active dates, or no
                usable cluster.

        Note:
            Unparsable resource dates are logged and treated as active.
        """
        if not self._authenticated:
            raise QAuthException("Unauthorized!!!")

        log.debug("Fetching LEXIS project resources of '%s'", self._lexis_project)
        try:
            project_resources = (
                await self._request(
                    "GET",
                    f"{self._lexis_userorg_api_url}/api/ProjectResource",
                    params={"ProjectShortName": self._lexis_project},
                    timeout=30,
                )
            ).json()
        except Exception as e:
            raise QAuthException.from_cause(
                reason=f"Failed to authorize LEXIS resource: {e!s}",
                user_id=self._username,
                resource=self._lexis_project,
            ) from e

        # A resource is quantum if any of its assignments is a quantum location.
        # (any() instead of a nested comprehension: one resource with N quantum
        #  assignments must not appear N times.)
        quantum_resources = [
            r
            for r in project_resources
            if any(
                self._is_quantum_assignment(a) for a in r.get("Assignments", []) or []
            )
        ]
        available_names = sorted(
            {r.get("Name") for r in quantum_resources if r.get("Name")}
        )
        log.debug(
            "LEXIS returned %d resource(s), quantum: %s",
            len(project_resources),
            available_names,
        )

        if not quantum_resources:
            raise QAuthException(
                reason=f"No quantum resource found for LEXIS project '{self._lexis_project}', "
                "please verify you have assigned one!",
                user_id=self._username,
                resource=self._lexis_project,
            )

        # Pick the resource: implicit only when unambiguous.
        if lexis_resource_name is None:
            if len(quantum_resources) > 1:
                raise QAuthException(
                    reason=f"Multiple quantum resources in project '{self._lexis_project}', "
                    f"specify lexis_resource_name. Available: {available_names}",
                    user_id=self._username,
                    resource=self._lexis_project,
                )
            resource = quantum_resources[0]
        else:
            resource = next(
                (r for r in quantum_resources if r.get("Name") == lexis_resource_name),
                None,
            )
            if resource is None:
                raise QAuthException(
                    reason=f"Quantum resource '{lexis_resource_name}' not found in project "
                    f"'{self._lexis_project}'. Available: {available_names}",
                    user_id=self._username,
                    resource=lexis_resource_name,
                )

        resource_name = resource.get("Name")
        start, end = resource.get("StartDate"), resource.get("EndDate")
        if start and end:
            try:
                in_range = to_utc(start) <= datetime.now(UTC) <= to_utc(end)
            except ValueError as date_error:
                # Lenient on purpose: bad dates must not lock the user out.
                log.warning("Could not parse resource dates: %s", date_error)
                in_range = True
            if not in_range:
                raise QAuthException(
                    reason=f"Resource '{resource_name}' is not within active dates ({start} - {end}).",
                    user_id=self._username,
                    resource=self._lexis_project,
                )

        # One QCluster per quantum assignment (keyed by LocationName).
        assignments = [
            a
            for a in resource.get("Assignments", []) or []
            if self._is_quantum_assignment(a)
        ]
        clusters: dict[str, QCluster] = {}
        for assignment in assignments:
            name = assignment.get("LocationName")
            heappe_url = self._spec_value(assignment, "HEAPPE_URL")
            if not name or not heappe_url:
                log.warning(
                    "Skipping assignment %s of resource '%s': missing LocationName or HEAPPE_URL",
                    assignment.get("Id"),
                    resource_name,
                )
                continue
            if name in clusters:
                log.debug(
                    "Duplicate assignment for cluster '%s', keeping the first", name
                )
                continue
            clusters[name] = QCluster(
                name=name,
                heappe_url=heappe_url,
                allocated_units=assignment.get("AllocationAmount"),
                aggregation_name=assignment.get("AggregationName"),
            )

        if not clusters:
            raise QAuthException(
                reason=f"Resource '{resource_name}' has no usable quantum cluster assignment "
                "(LocationName + HEAPPE_URL).",
                user_id=self._username,
                resource=resource_name,
            )

        log.debug(
            "Resource '%s' clusters: %s",
            resource_name,
            {n: c.heappe_url for n, c in sorted(clusters.items())},
        )
        return LexisResource(
            resource_name=resource_name,
            # All assignments of one resource share the ProjectResourceId.
            project_resource_id=assignments[0].get("ProjectResourceId"),
            resource_validity_start=start,
            resource_validity_end=end,
            clusters=clusters,
        )

    # =================================================================
    # Cluster discovery, project ids & machine info
    # =================================================================

    def _matches_resource(self, project: Any) -> bool:
        """
        Whether a HEAppE project belongs to the LEXIS resource.

        Matches the resource name against the project's ``name`` or ``accounting_string``.

        Args:
            project (Any): ``py4heappe`` project model from ``ClusterNodeTypeExt.projects``.

        Returns:
            bool
        """
        name = self.lexis_resource.resource_name
        return name in (
            getattr(project, "name", None),
            getattr(project, "accounting_string", None),
        )

    async def _list_available_clusters(self, url: str) -> list[ClusterExt]:
        """
        Async equivalent of ``ClusterInformationApi.heappe_cluster_information_list_available_clusters_get``.

        Args:
            url (str): HEAppE base URL.

        Returns:
            list[ClusterExt]: Clusters visible to the user, with node types and projects.

        Raises:
            QException: Request failed.
        """
        res = await self._request(
            "GET", self._join(url, "/heappe/ClusterInformation/ListAvailableClusters")
        )
        return from_swagger_json(res.json() or [], "list[ClusterExt]")

    async def _resolve_heappe_instance(self, url: str, wanted: list[QCluster]) -> None:
        """
        Resolve HEAppE IDs of all clusters served by one HEAppE instance.

        Issues a single ``ListAvailableClusters`` call and fills ``cluster_id``,
        ``cluster_node_type_id`` and, if visible, ``heappe_project_id`` on each
        cluster. Never raises: problems are written to ``cluster.error``
        (and ``available=False`` if the instance is unreachable).

        Args:
            url (str): HEAppE base URL.
            wanted (list[QCluster]): Clusters behind ``url``.

        Note:
            Only the first node type of a cluster is used.
        """
        log.debug("Resolving clusters %s at HEAppE %s", [c.name for c in wanted], url)
        try:
            heappe_clusters = await self._list_available_clusters(url)
        except Exception as e:  # noqa: BLE001
            for cluster in wanted:
                cluster.available = False
                cluster.error = f"ListAvailableClusters failed on {url}: {e!s}"
            log.warning("ListAvailableClusters failed on %s: %s", url, e)
            return

        by_name: dict[str, ClusterExt] = {hc.name: hc for hc in heappe_clusters}
        log.debug("HEAppE %s exposes clusters: %s", url, sorted(by_name))
        for cluster in wanted:
            hc = by_name.get(cluster.name)
            if hc is None:
                cluster.error = (
                    f"Cluster '{cluster.name}' is not exposed by HEAppE at {url}"
                )
                continue
            cluster.cluster_id = hc.id
            node_types = hc.node_types or []
            if not node_types:
                cluster.error = f"Cluster '{cluster.name}' has no node types in HEAppE"
                continue
            if len(node_types) > 1:
                log.warning(
                    "Cluster '%s' has %d node types, using the first (%s)",
                    cluster.name,
                    len(node_types),
                    node_types[0].id,
                )
            node_type = node_types[0]
            cluster.cluster_node_type_id = node_type.id
            # ProjectId may be absent here; _project_id_for() falls back to a lookup.
            project = next(
                (
                    p
                    for p in getattr(node_type, "projects", None) or []
                    if self._matches_resource(p)
                ),
                None,
            )
            if project is not None:
                cluster.heappe_project_id = project.id
            log.debug(
                "Cluster '%s': cluster_id=%s node_type_id=%s heappe_project_id=%s",
                cluster.name,
                cluster.cluster_id,
                cluster.cluster_node_type_id,
                cluster.heappe_project_id
                if cluster.heappe_project_id is not None
                else "<lookup via accounting string>",
            )

    async def _resolve_heappe_clusters(self) -> None:
        """
        Resolve HEAppE IDs for every cluster of the resource.

        Clusters are grouped by HEAppE URL and instances are queried
        concurrently. Failures are recorded on the cluster (``available=False``,
        ``error=...``) instead of aborting :meth:`_connect`.
        """
        by_url: dict[str, list[QCluster]] = {}
        for cluster in self.lexis_resource.clusters.values():
            by_url.setdefault(cluster.heappe_url, []).append(cluster)
        await asyncio.gather(
            *(self._resolve_heappe_instance(u, w) for u, w in by_url.items())
        )

    async def _project_id_for(self, cluster: QCluster) -> int:
        """
        HEAppE ProjectId of the cluster.

        Taken from ``ListAvailableClusters`` when present; otherwise looked up
        once per HEAppE instance via ``ProjectsByAccountingStrings`` with the
        LEXIS resource name. A per-URL lock prevents duplicate concurrent
        lookups, and the result is cached on the cluster and per URL.

        Args:
            cluster (QCluster): Target cluster.

        Returns:
            int

        Raises:
            QException: Lookup failed or no project matches.
        """
        if cluster.heappe_project_id is not None:
            return cluster.heappe_project_id

        url = cluster.heappe_url
        lock = self._project_locks.setdefault(
            (url, asyncio.get_running_loop()), asyncio.Lock()
        )
        async with lock:
            # Re-check under the lock: another task may have filled the cache.
            if url not in self._heappe_project_ids:
                log.debug(
                    "Looking up HEAppE project for accounting string '%s' at %s",
                    self.lexis_resource.resource_name,
                    url,
                )
                projects = (
                    await self._request(
                        "GET",
                        self._join(
                            url, "/heappe/Management/ProjectsByAccountingStrings"
                        ),
                        params={
                            "AccountingString": [self.lexis_resource.resource_name]
                        },
                    )
                ).json()
                if not projects:
                    raise QException(
                        f"Project for accounting string '{self.lexis_resource.resource_name}' "
                        f"not found in HEAppE at {url}."
                    )
                self._heappe_project_ids[url] = projects[0].get("Id")
        cluster.heappe_project_id = self._heappe_project_ids[url]
        return cluster.heappe_project_id

    async def _ids_for(self, backend_name: str | None) -> tuple[QCluster, int, int]:
        """
        Cluster and HEAppE IDs needed for job and session calls.

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            tuple[QCluster, int, int]: ``(cluster, cluster_id, project_id)``.

        Raises:
            QException: Backend unknown, ClusterId unresolved, or project lookup failed.
        """
        cluster = self._resolve_cluster(backend_name)
        if cluster.cluster_id is None:
            raise QException(
                f"HEAppE ClusterId not resolved for backend '{cluster.name}'."
            )
        return cluster, cluster.cluster_id, await self._project_id_for(cluster)

    async def _fetch_machine_info(self, cluster: QCluster) -> None:
        """
        Fetch ``/heappe/QScheduler/MachineInfo`` and store it on the cluster.

        The cluster is marked available only if the response contains a software
        stack listed in :attr:`SUPPORTED_SW_STACKS` and a QScheduler version.
        Never raises; the outcome is reflected in ``cluster.available`` and
        ``cluster.error``.

        Args:
            cluster (QCluster): Target cluster.
        """
        if cluster.cluster_node_type_id is None:
            # HEAppE resolution failed earlier; error is already set.
            cluster.available = False
            return
        try:
            res = await self._heappe_get(
                cluster,
                "/heappe/QScheduler/MachineInfo",
                {
                    "ClusterNodeTypeId": cluster.cluster_node_type_id,
                    "ProjectId": await self._project_id_for(cluster),
                },
            )
            info = QMachineInfo.from_response(res.json())
            if not info.sw_stack or not info.qscheduler_version:
                raise QException(
                    "incomplete MachineInfo: missing type or qscheduler_version"
                )
            if info.sw_stack not in self.SUPPORTED_SW_STACKS:
                raise QException(f"unsupported backend type '{info.sw_stack}'")
            cluster.machine_info = info
            cluster.available = True
            log.debug(
                "MachineInfo '%s': type=%s qscheduler=%s qccsw=%s",
                cluster.name,
                info.sw_stack,
                info.qscheduler_version,
                info.qccsw_version,
            )
            # TODO: extend with availability of qccsw. Should be implemented in HEAppE.
            cluster.error = None
        except Exception as e:  # noqa: BLE001
            cluster.available = False
            cluster.error = f"MachineInfo failed: {e!s}"
            log.warning("Backend '%s' unavailable: %s", cluster.name, cluster.error)

    async def _load_machine_info_all(self) -> None:
        """Fetch machine info of all resolved clusters concurrently; never raises."""
        clusters = [
            c
            for c in self.lexis_resource.clusters.values()
            if c.cluster_node_type_id is not None
        ]
        await asyncio.gather(*(self._fetch_machine_info(c) for c in clusters))

    async def refresh_machine_info(self, backend_name: str | None = None) -> None:
        """
        Re-fetch machine info and update availability.

        Args:
            backend_name (str or None): Backend to refresh; all backends if ``None``.

        Raises:
            QException: ``backend_name`` is unknown.
        """
        if backend_name is None:
            await self._load_machine_info_all()
        else:
            await self._fetch_machine_info(self._resolve_cluster(backend_name))

    def _resolve_cluster(self, backend_name: str | None = None) -> QCluster:
        """
        Look up a cluster by name, or return the active one.

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            QCluster

        Raises:
            QException: Unknown backend, or ``None`` with no active backend.
        """
        clusters = self.lexis_resource.clusters
        if backend_name is None:
            if self._active_cluster is not None:
                return clusters[self._active_cluster]
            raise QException(
                f"No backend selected; call get_backend(name). Available: {sorted(clusters)}"
            )
        try:
            return clusters[backend_name]
        except KeyError:
            raise QException(
                f"Unknown backend '{backend_name}'. Available: {sorted(clusters)}"
            ) from None

    # =================================================================
    # Backend selection
    # =================================================================

    async def _validate_backend(self, backend_name: str | None = None) -> QCluster:
        """
        Resolve a cluster and ensure it is usable; the active backend is not changed.

        Machine info is fetched on demand if missing (lazy mode or retry after an
        earlier failure).

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            QCluster

        Raises:
            QAuthException: Client not authenticated.
            QException: Backend unknown or unavailable.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        cluster = self._resolve_cluster(backend_name)
        if cluster.machine_info is None:
            # lazy path (load_machine_info=False) or retry after an earlier failure
            await self._fetch_machine_info(cluster)
        if not cluster.available:
            raise QException(
                f"Backend '{cluster.name}' is not available: {cluster.error}"
            )
        # self._verify_backend_version(cluster)  # FIXME: Uncomment and finalize
        return cluster

    async def get_backend(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> QBackendMetadata:
        """
        Select a backend and return its metadata.

        The selected backend becomes the default for methods called without
        ``backend_name``. For IQM backends the metadata includes the static and
        dynamic quantum architecture (see :meth:`_metadata`).

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.
            calibration_set_id (UUID or str or None): IQM calibration set for the dynamic architecture;
                current calibration if ``None``.

        Returns:
            QBackendMetadata

        Raises:
            QAuthException: Client not authenticated.
            QException: Backend unknown or unavailable.
        """
        cluster = await self._validate_backend(backend_name)
        self._active_cluster = cluster.name
        return await self._metadata(cluster, calibration_set_id)

    def bind(self, backend_name: str | None = None) -> QClient:
        """
        Return a client view pinned to one backend (sync API).

        The view is a shallow copy sharing the HTTP session, event loop, LEXIS
        resource, clusters and caches, with its own active backend. Closing a
        view does not close the shared session or loop.

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            QClient

        Raises:
            QAuthException: Client not authenticated.
            QException: Backend unknown or unavailable, or called from the loop
                owning the HTTP session.
        """
        cluster = self._run(self._validate_backend(backend_name))
        view = copy.copy(self)
        view._active_cluster = cluster.name
        view._is_view = True
        view._owns_session = False
        return view

    async def aget_available_backends(
        self, with_architecture: bool = True
    ) -> dict[str, QBackendMetadata]:
        """
        Retrieve metadata for all available backends of the resource.

        This method returns metadata for every backend in the LEXIS quantum resource,
        including those that are currently unavailable. Each entry's cluster status
        (available or not) is checked via ``cluster.available`` and ``cluster.error``.
        Args:
            with_architecture (bool): If True, fetch IQM architectures concurrently.
                When False, skips architecture details to reduce network overhead.
        Returns:
            dict[str, QBackendMetadata]: A dictionary mapping backend names to their metadata,
                sorted alphabetically by name. Each entry includes basic cluster information
                and availability status.

        Raises:
            QAuthException: If the client is not authenticated.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        items = sorted(self.lexis_resource.clusters.items())
        if with_architecture:
            mds = await asyncio.gather(*(self._metadata(c) for _, c in items))
        else:
            mds = [self._plain_metadata(c) for _, c in items]
        return dict(zip((n for n, _ in items), mds))

    def get_available_backends(
        self, with_architecture: bool = True
    ) -> dict[str, QBackendMetadata]:
        """
        Retrieve metadata for all available backends of the resource.

        This method provides a synchronous wrapper around `aget_available_backends`.
        It returns metadata for every backend in the LEXIS quantum resource,
        including those that are currently unavailable. Each entry's cluster status
        (available or not) is checked via ``cluster.available`` and ``cluster.error``.
            Args:
            with_architecture (bool): If True, fetches IQM architectures concurrently.
                When False, skips architecture details to reduce network overhead.

        Returns:
            dict[str, QBackendMetadata]: A dictionary mapping backend names to their metadata,
                sorted alphabetically by name. Each entry includes basic cluster information
                and availability status.
            Raises:
            QAuthException: If the client is not authenticated.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")

        return self._run(
            self.aget_available_backends(with_architecture=with_architecture)
        )

    async def _get_quantum_backend_info(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> QBackendMetadata:
        """
        Backend metadata without selecting or validating the backend (async).

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.
            calibration_set_id (UUID or str or None): IQM calibration set; current if ``None``.

        Returns:
            QBackendMetadata

        Raises:
            QAuthException: Client not authenticated.
            QException: Backend unknown or none selected.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        return await self._metadata(
            self._resolve_cluster(backend_name), calibration_set_id
        )

    def get_quantum_backend_info(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> QBackendMetadata:
        """
        Sync variant of :meth:`_get_quantum_backend_info`.

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.
            calibration_set_id (UUID or str or None): IQM calibration set; current if ``None``.

        Returns:
            QBackendMetadata

        Raises:
            QAuthException: Client not authenticated.
            QException: Backend unknown, none selected, or called from the loop
                owning the HTTP session.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        return self._run(
            self._metadata(self._resolve_cluster(backend_name), calibration_set_id)
        )

    def _plain_metadata(self, cluster: QCluster) -> QBackendMetadata:
        """
        Metadata without architecture information (no I/O).

        Args:
            cluster (QCluster): Target cluster.

        Returns:
            QBackendMetadata
        """
        return QBackendMetadata(self._lexis_project_obj, self.lexis_resource, cluster)

    async def _metadata(
        self, cluster: QCluster, calibration_set_id: UUID | str | None = None
    ) -> QBackendMetadata:
        """
        Backend metadata, enriched with IQM architectures when applicable.

        For IQM backends the static architecture (``MachineArchitecture``) and the
        dynamic architecture of the calibration set (``MachineCalibration``) are
        fetched concurrently. The static architecture response is a list; its
        first element is used. Failures are logged and plain metadata is returned.

        Args:
            cluster (QCluster): Target cluster.
            calibration_set_id (UUID or str or None): IQM calibration set; current if ``None``.

        Returns:
            QBackendMetadata
        """
        if not (cluster.machine_info and cluster.machine_info.sw_stack == "iqm"):
            return self._plain_metadata(cluster)
        log.debug(
            "Fetching architecture of '%s' (calibration_set_id=%s)",
            cluster.name,
            calibration_set_id or "default",
        )
        try:
            # Imported lazily: iqm-client is only needed for IQM backends.
            from iqm.iqm_client import (
                DynamicQuantumArchitecture,
                StaticQuantumArchitecture,
            )

            static_raw, dynamic_raw = await asyncio.gather(
                self.machine_architecture(cluster=cluster),
                self.machine_calibration(
                    cluster=cluster,
                    calibration_set_id=calibration_set_id,
                    endpoint=MachineCalibrationIQMEndpoint.DYNAMIC_QUANTUM_ARCHITECTURE,
                ),
            )
            dynamic_arch = DynamicQuantumArchitecture.model_validate_json(dynamic_raw)
            _static_arch = json.loads(static_raw)
            # Endpoint returns a list of architectures; only the first is used.
            _static_arch = (
                _static_arch[0]
                if isinstance(_static_arch, list) and len(_static_arch) > 0
                else {}
            )
            static_arch = StaticQuantumArchitecture.model_validate(_static_arch)

        except Exception as e:  # noqa: BLE001
            log.warning(
                "Failed to fetch architecture for backend '%s': %s: %s",
                cluster.name,
                type(e).__name__,
                e,
            )
            return self._plain_metadata(cluster)
        return QBackendMetadata(
            lexis_project=self._lexis_project_obj,
            lexis_resource=self.lexis_resource,
            cluster=cluster,
            static_architecture=static_arch,
            dynamic_architecture=dynamic_arch,
        )

    # =================================================================
    # Properties
    # =================================================================

    @property
    def heappe_client(self) -> HEAppEApi | None:
        """
        Sync ``py4heappe`` API client of the active backend, created lazily.

        Not used by :class:`QClient` itself; provided for callers needing the
        generated API. Calls through it block the event loop.

        Returns:
            HEAppEApi or None: Client, or ``None`` if no backend is active.
        """
        if self._active_cluster is None:
            return None
        url = self._resolve_cluster().heappe_url
        if url not in self._heappe_clients:
            conf = HEAppEConfiguration()
            conf.host = url
            self._heappe_clients[url] = HEAppEApi(
                conf, header_name="Authorization", header_value=f"Bearer {self._token}"
            )
        return self._heappe_clients[url]

    @property
    def cluster_information_api(self) -> ClusterInformationApi | None:
        """
        Sync ``ClusterInformationApi`` of the active backend (blocking; not for async code).

        Returns:
            ClusterInformationApi or None
        """
        client = self.heappe_client
        return ClusterInformationApi(client) if client is not None else None

    @property
    def job_management_api(self) -> JobManagementApi | None:
        """
        Sync ``JobManagementApi`` of the active backend (blocking; not for async code).

        Returns:
            JobManagementApi or None
        """
        client = self.heappe_client
        return JobManagementApi(client) if client is not None else None

    @property
    def session(self) -> aiohttp.ClientSession | None:
        """
        Underlying HTTP session, or ``None`` if not connected.

        Returns:
            aiohttp.ClientSession or None
        """
        return self._session

    @property
    def is_authenticated(self) -> bool:
        """
        Whether :meth:`_connect` succeeded and the client has not been closed.

        Returns:
            bool
        """
        return self._authenticated and self._token is not None

    @property
    def lexis_project(self) -> str:
        """
        LEXIS project short name.

        Returns:
            str
        """
        return self._lexis_project

    @property
    def lexis_resource(self) -> LexisResource:
        """
        Authorized LEXIS resource with its clusters.

        Returns:
            LexisResource

        Raises:
            QException: Client not connected.
        """
        if self._lexis_resource is None:
            raise QException("Client not connected; call connect() first")
        return self._lexis_resource

    @property
    def backend_names(self) -> list[str]:
        """
        Names of all backends (clusters) of the resource.

        Returns:
            list[str]

        Raises:
            QException: Client not connected.
        """
        return self.lexis_resource.cluster_names

    @property
    def active_backend(self) -> str | None:
        """
        Name of the active backend, or ``None`` if none is selected.

        Returns:
            str or None
        """
        return self._active_cluster

    @property
    def cluster_id(self) -> int | None:
        """
        HEAppE ClusterId of the active backend.

        Returns:
            int or None
        """
        return self._resolve_cluster().cluster_id if self._active_cluster else None

    @property
    def project_id(self) -> int | None:
        """
        HEAppE ProjectId of the active backend, if already resolved (no I/O).

        Returns:
            int or None
        """
        return (
            self._resolve_cluster().heappe_project_id if self._active_cluster else None
        )

    # =================================================================
    # QScheduler: job submission & results
    # =================================================================

    async def submit_quantum_job(
        self,
        tasks: list[dict[str, Any]],
        job_name: str = "quantum_job",
        circuit_payloads: dict[str, bytes | str] | None = None,
        backend_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Create and submit a job via ``POST /heappe/QScheduler/CreateAndSubmitJob``.

        The request is ``multipart/form-data`` with a JSON ``model`` part and one
        file part per task whose ``PayloadPartName`` is a key of
        ``circuit_payloads``. ``ClusterId`` and ``ProjectId`` are filled from the
        backend.

        Args:
            tasks (list[dict[str, Any]]): QScheduler task specifications, e.g.::

                [{"Name": "GroverTask", "MachineId": "iqm_simulator",
                  "WalltimeLimitSecs": 3600, "PayloadPartName": "circuit1",
                  "UseSessions": False}]
            job_name (str): HEAppE job name.
            circuit_payloads (dict[str, bytes | str] or None): Maps a task's ``PayloadPartName`` to the file
                content uploaded with the job; ``str`` is encoded as UTF-8.
            backend_name (str or None): Target backend; active backend if ``None``.

        Returns:
            dict[str, Any]: ``SubmittedJobInfoExt`` as a dict (``Id``, ``Name``, ``State``,
                ``Tasks[...]``).

        Raises:
            QAuthException: Client not authenticated.
            QException: IDs not resolved or submission failed.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        cluster, cluster_id, project_id = await self._ids_for(backend_name)

        model = {
            "SessionCode": "",  # unused, Bearer JWT auth is used instead
            "JobSpecification": {
                "Name": job_name,
                "ClusterId": cluster_id,
                "ProjectId": project_id,
                "Tasks": tasks,
            },
        }

        log.debug(
            "Submitting job '%s' to '%s' (cluster_id=%s project_id=%s): tasks=%s",
            job_name,
            cluster.name,
            cluster_id,
            project_id,
            [
                (t.get("Name"), t.get("MachineId"), t.get("PayloadPartName"))
                for t in tasks
            ],
        )

        # Explicit MultipartWriter: FormData would fall back to urlencoded without files
        with aiohttp.MultipartWriter("form-data") as mp:
            part = mp.append(json.dumps(model), {"Content-Type": "application/json"})
            part.set_content_disposition("form-data", name="model")
            # Attach payloads only for tasks that reference one; unknown names are ignored.
            for task in tasks:
                part_name = task.get("PayloadPartName")
                if part_name and circuit_payloads and part_name in circuit_payloads:
                    content = circuit_payloads[part_name]
                    if isinstance(content, str):
                        content = content.encode("utf-8")
                    fpart = mp.append(
                        content, {"Content-Type": "application/octet-stream"}
                    )
                    fpart.set_content_disposition(
                        "form-data", name=part_name, filename=part_name
                    )

            try:
                res = await self._request(
                    "POST",
                    self._join(
                        cluster.heappe_url, "/heappe/QScheduler/CreateAndSubmitJob"
                    ),
                    data=mp,
                    timeout=120,
                )
            except QException as e:
                raise QException(
                    f"Failed to submit quantum job to '{cluster.name}': {e!s}"
                ) from e
        submitted = res.json()
        if isinstance(submitted, dict):
            log.debug(
                "Submitted job id=%s state=%s task_ids=%s",
                submitted.get("Id"),
                submitted.get("State"),
                [t.get("Id") for t in submitted.get("Tasks", []) or []],
            )

        return submitted

    async def aget_job_info(
        self, job_id: int, backend_name: str | None = None
    ) -> dict[str, Any]:
        """
        Current job state via ``GET /heappe/JobManagement/CurrentInfoForJob``.

        Args:
            job_id (int): HEAppE job ID.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            dict[str, Any]: ``SubmittedJobInfoExt`` as a dict.

        Raises:
            QException: Request failed.
        """
        log.debug("Fetching info of job %s", job_id)
        res = await self._request(
            "GET",
            self._heappe_url("/heappe/JobManagement/CurrentInfoForJob", backend_name),
            params={"SubmittedJobInfoId": int(job_id)},
        )
        return res.json()

    def get_job_info(
        self, job_id: int, backend_name: str | None = None
    ) -> dict[str, Any]:
        """
        Sync wrapper of :meth:`aget_job_info`.

        Args:
            job_id (int): HEAppE job ID.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            dict[str, Any]

        Raises:
            QException: Request failed or called from the loop owning the HTTP session.
        """
        return self._run(self.aget_job_info(job_id, backend_name))

    async def get_task_result(
        self, task_id: int, backend_name: str | None = None
    ) -> Any:
        """
        Task result via ``GET /heappe/QScheduler/GetTaskResult``.

        For IQM backends this contains job metadata, timeline, etc.

        Args:
            task_id (int): HEAppE task ID.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            Any: Decoded JSON, or raw bytes for non-JSON responses.

        Raises:
            QResultsFailed: Request failed.
        """
        try:
            res = await self._request(
                "GET",
                self._heappe_url("/heappe/QScheduler/GetTaskResult", backend_name),
                params={"SubmittedTaskId": task_id},
            )
        except QException as e:
            raise QResultsFailed(task_id, f"Failed to fetch task result: {e!s}") from e
        return res.parsed()

    async def get_task_artifact(
        self, task_id: int, artifact_name: str, backend_name: str | None = None
    ) -> Any:
        """
        Named task artifact via ``GET /heappe/QScheduler/GetTaskArtifact``.

        Args:
            task_id (int): HEAppE task ID.
            artifact_name (str): Artifact name, e.g. ``measurements``,
                ``measurements_counts``, ``sweep_results``.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            Any: Decoded JSON, or raw bytes for non-JSON responses.

        Raises:
            QResultsFailed: Request failed.
        """
        try:
            res = await self._request(
                "GET",
                self._heappe_url("/heappe/QScheduler/GetTaskArtifact", backend_name),
                params={"SubmittedTaskId": task_id, "ArtifactName": artifact_name},
            )
        except QException as e:
            raise QResultsFailed(
                task_id, f"Failed to fetch task artifact '{artifact_name}': {e!s}"
            ) from e
        return res.parsed()

    async def aget_job_results(
        self,
        submitted_job: dict[str, Any],
        artifact_names: list[str] | None = None,
        backend_name: str | None = None,
        max_concurrency: int = 8,
    ) -> dict[int, dict[str, Any]]:
        """
        Fetch the result and optional artifacts of every task of a job, concurrently.

        The job should be in a terminal state, e.g. after
        :meth:`await_for_job_terminal_state`.

        Args:
            submitted_job (dict[str, Any]): Job info as returned by :meth:`submit_quantum_job`
                or :meth:`aget_job_info`; only ``Tasks[].Id`` is used.
            artifact_names (list[str] or None): Artifacts to fetch for every task.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.
            max_concurrency (int): Maximum number of requests in flight.

        Returns:
            dict[int, dict[str, Any]]: ``{task_id: {"result": ..., "artifacts": {name: ...}}}``;
                ``"artifacts"`` is present only if ``artifact_names`` is non-empty.

        Raises:
            QResultsFailed: Any result or artifact request failed.
        """
        sem = asyncio.Semaphore(max_concurrency)

        async def limited(coro):
            # Limits the number of concurrent executions of the coroutine using a semaphore.
            # This helps in controlling the load and preventing overwhelming the server.
            async with sem:
                return await coro

        async def one_task(task_id: int) -> tuple[int, dict[str, Any]]:
            # Result and all artifacts of one task, fetched in parallel.
            names = artifact_names or []
            result, *artifacts = await asyncio.gather(
                limited(self.get_task_result(task_id, backend_name)),
                *(
                    limited(self.get_task_artifact(task_id, n, backend_name))
                    for n in names
                ),
            )
            entry: dict[str, Any] = {"result": result}
            if names:
                entry["artifacts"] = dict(zip(names, artifacts, strict=True))
            return task_id, entry

        task_ids = [t["Id"] for t in submitted_job.get("Tasks", [])]
        log.debug(
            "Fetching results of job %s: tasks=%s artifacts=%s",
            submitted_job.get("Id"),
            task_ids,
            artifact_names or [],
        )
        pairs = await asyncio.gather(*(one_task(tid) for tid in task_ids))
        return dict(pairs)

    def get_job_results(
        self,
        submitted_job: dict[str, Any],
        artifact_names: list[str] | None = None,
        backend_name: str | None = None,
        max_concurrency: int = 8,
    ) -> dict[int, dict[str, Any]]:
        """
        Sync wrapper of :meth:`aget_job_results`.

        Args:
            submitted_job (dict[str, Any]): Job info with ``Tasks[].Id``.
            artifact_names (list[str] or None): Artifacts to fetch for every task.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.
            max_concurrency (int): Maximum number of requests in flight.

        Returns:
            dict[int, dict[str, Any]]

        Raises:
            QResultsFailed: Any result or artifact request failed.
            QException: Called from the loop owning the HTTP session.
        """
        return self._run(
            self.aget_job_results(
                submitted_job, artifact_names, backend_name, max_concurrency
            )
        )

    # =================================================================
    # QScheduler: machine info
    # =================================================================

    async def machine_architecture(
        self,
        cluster_node_type_id: int | None = None,
        project_id: int | None = None,
        backend_name: str | None = None,
        cluster: QCluster | None = None,
    ) -> str:
        """
        Static machine architecture via ``GET /heappe/QScheduler/MachineArchitecture``.

        For IQM this is a JSON list of static quantum architectures (qubits,
        connectivity).

        Args:
            cluster_node_type_id (int or None): Override of the cluster's node type ID.
            project_id (int or None): Override of the HEAppE ProjectId.
            backend_name (str or None): Backend name; active backend if ``None``. Ignored if
                ``cluster`` is given.
            cluster (QCluster or None): Cluster object (internal shortcut).

        Returns:
            str: Raw response text.

        Raises:
            QException: Backend unknown or request failed.
        """
        cluster = cluster or self._resolve_cluster(backend_name)
        res = await self._heappe_get(
            cluster,
            "/heappe/QScheduler/MachineArchitecture",
            {
                "ClusterNodeTypeId": cluster_node_type_id
                or cluster.cluster_node_type_id,
                "ProjectId": project_id
                if project_id is not None
                else await self._project_id_for(cluster),
            },
        )
        return res.text()

    async def machine_calibration(
        self,
        cluster_node_type_id: int | None = None,
        calibration_set_id: UUID | str | None = None,
        endpoint: MachineCalibrationIQMEndpoint
        | str
        | None = MachineCalibrationIQMEndpoint.METRICS,
        project_id: int | None = None,
        backend_name: str | None = None,
        cluster: QCluster | None = None,
    ) -> str:
        """
        Calibration data via ``GET /heappe/QScheduler/MachineCalibration``.

        Args:
            cluster_node_type_id (int or None): Override of the cluster's node type ID.
            calibration_set_id (UUID or str or None): Calibration set; for IQM ``None`` means the
                current calibration (``"default"``).
            endpoint (MachineCalibrationIQMEndpoint or str or None): Calibration endpoint, see :class:`MachineCalibrationIQMEndpoint`.
            project_id (int or None): Override of the HEAppE ProjectId.
            backend_name (str or None): Backend name; active backend if ``None``. Ignored if
                ``cluster`` is given.
            cluster (QCluster or None): Cluster object (internal shortcut).

        Returns:
            str: Raw response text (JSON for IQM endpoints).

        Raises:
            QException: Backend unknown or request failed.
        """
        cluster = cluster or self._resolve_cluster(backend_name)
        sw_stack = cluster.machine_info.sw_stack if cluster.machine_info else None
        # IQM expects an explicit "default" alias for the current calibration set.
        if sw_stack == "iqm" and calibration_set_id is None:
            calibration_set_id = "default"
        res = await self._heappe_get(
            cluster,
            "/heappe/QScheduler/MachineCalibration",
            {
                "ClusterNodeTypeId": cluster_node_type_id
                or cluster.cluster_node_type_id,
                "CalibrationId": calibration_set_id,  # UUID/Enum normalized by 'normalize_query'
                "Endpoint": endpoint,
                "ProjectId": project_id
                if project_id is not None
                else await self._project_id_for(cluster),
            },
        )
        return res.text()

    async def machine_metrics(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """
        Calibration quality metrics of a backend as parsed JSON.

        Args:
            backend_name (str or None): Target backend; active backend if ``None``.
            calibration_set_id (UUID or str or None): IQM calibration set; current (``"default"``) if ``None``.

        Returns:
            dict[str, Any]: Metrics (fidelities, coherence times, ...), as returned by the backend.

        Raises:
            QException: Backend unknown, request failed or response is not JSON.
        """
        cluster = self._resolve_cluster(backend_name)
        raw = await self.machine_calibration(
            cluster=cluster,
            calibration_set_id=calibration_set_id,
            endpoint=MachineCalibrationIQMEndpoint.METRICS,
        )
        try:
            metrics = json.loads(raw)
        except ValueError:
            raise QException(
                f"Metrics of backend '{cluster.name}' are not valid JSON: {req_body_preview(raw, 200)}"
            ) from None
        log.debug(
            "Metrics of '%s': %s",
            cluster.name,
            f"{len(metrics)} entries"
            if isinstance(metrics, (dict, list))
            else type(metrics).__name__,
        )
        return metrics

    def get_machine_metrics(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """
        Sync wrapper of :meth:`machine_metrics`.

        Args:
            backend_name (str or None): Target backend; active backend if ``None``.
            calibration_set_id (UUID or str or None): IQM calibration set; current if ``None``.

        Returns:
            dict[str, Any]

        Raises:
            QException: Request failed, invalid JSON, or called from the loop
                owning the HTTP session.
        """
        return self._run(self.machine_metrics(backend_name, calibration_set_id))

    # =================================================================
    # QScheduler: sessions
    # =================================================================

    async def open_qscheduler_session(
        self, machine_id: str, walltime_limit_secs: int, backend_name: str | None = None
    ) -> int:
        """
        Open a QScheduler session via ``POST /heappe/QScheduler/OpenSession``.

        A session reserves the machine so that tasks submitted with
        ``UseSessions=True`` run back to back without re-queuing.

        Args:
            machine_id (str): QScheduler machine ID, e.g. ``"iqm_simulator"``.
            walltime_limit_secs (int): Maximum session duration in seconds.
            backend_name (str or None): Target backend; active backend if ``None``.

        Returns:
            int: Session ID.

        Raises:
            QException: IDs not resolved or request failed.
        """
        cluster, cluster_id, project_id = await self._ids_for(backend_name)
        res = await self._request(
            "POST",
            self._join(cluster.heappe_url, "/heappe/QScheduler/OpenSession"),
            json_body={
                "ClusterId": cluster_id,
                "ProjectId": project_id,
                "MachineId": machine_id,
                "WalltimeLimit": walltime_limit_secs,
            },
        )
        return res.json()

    async def close_qscheduler_session(
        self, session_id: int, backend_name: str | None = None
    ) -> None:
        """
        Close a QScheduler session via ``DELETE /heappe/QScheduler/CloseSession``.

        Args:
            session_id (int): Session ID from :meth:`open_qscheduler_session`.
            backend_name (str or None): Target backend; active backend if ``None``.

        Raises:
            QException: IDs not resolved or request failed.
        """
        cluster, cluster_id, project_id = await self._ids_for(backend_name)
        try:
            await self._request(
                "DELETE",
                self._join(cluster.heappe_url, "/heappe/QScheduler/CloseSession"),
                json_body={
                    "ClusterId": cluster_id,
                    "ProjectId": project_id,
                    "SessionId": session_id,
                },
                timeout=30,
            )
        except QException as e:
            raise QException(
                f"Failed to close QScheduler session {session_id}: {e!s}"
            ) from e

    async def get_qscheduler_session_info(
        self, session_id: int, backend_name: str | None = None
    ) -> dict[str, Any]:
        """
        Session details via ``GET /heappe/QScheduler/GetSessionInfo``.

        Args:
            session_id (int): Session ID.
            backend_name (str or None): Target backend; active backend if ``None``.

        Returns:
            dict[str, Any]

        Raises:
            QException: Request failed.
        """
        res = await self._request(
            "GET",
            self._heappe_url("/heappe/QScheduler/GetSessionInfo", backend_name),
            params={"SessionId": session_id},
        )
        return res.json()

    async def list_qscheduler_sessions(
        self,
        state: str | None = None,
        cluster_id: int | None = None,
        project_id: int | None = None,
        backend_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List sessions via ``GET /heappe/QScheduler/ListSessions``.

        Args:
            state (str or None): Filter by session state; all states if ``None``.
            cluster_id (int or None): Override of the HEAppE ClusterId.
            project_id (int or None): Override of the HEAppE ProjectId.
            backend_name (str or None): Target backend; active backend if ``None``.

        Returns:
            list[dict[str, Any]]

        Raises:
            QException: Request failed.
        """
        cluster = self._resolve_cluster(backend_name)
        res = await self._heappe_get(
            cluster,
            "/heappe/QScheduler/ListSessions",
            {
                "State": state,
                "ClusterId": cluster_id
                if cluster_id is not None
                else cluster.cluster_id,
                "ProjectId": project_id
                if project_id is not None
                else await self._project_id_for(cluster),
            },
        )
        return res.json()

    # =================================================================
    # Status via /heappe/Events (websocket, CloudEvents)
    # =================================================================

    def _events_ws_url(self, backend_name: str | None = None) -> str:
        """
        WebSocket URL of ``/heappe/Events`` (``http(s)`` mapped to ``ws(s)``).

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.

        Returns:
            str
        """
        base = self._resolve_cluster(backend_name).heappe_url.rstrip("/")
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/heappe/Events"

    @staticmethod
    def _ws_close_timeout(seconds: float = 5.0) -> dict[str, Any]:
        """
        ``ws_connect`` kwargs bounding the close handshake.

        Uses ``ClientWSTimeout`` on :mod:`aiohttp` >= 3.11, a plain float otherwise.

        Args:
            seconds (float): Close handshake timeout.

        Returns:
            dict[str, Any]
        """
        ws_timeout = getattr(aiohttp, "ClientWSTimeout", None)
        if ws_timeout is not None:
            return {"timeout": ws_timeout(ws_close=seconds)}
        return {"timeout": seconds}

    async def iter_events(
        self, backend_name: str | None = None, heartbeat: float = 30.0
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield decoded CloudEvents from the backend's ``/heappe/Events`` websocket.

        Events cover all jobs visible to the user; filter by ``data.jobId``.
        Empty and non-JSON messages are skipped. The iterator ends when the
        server closes the socket. Wrap the loop in :func:`asyncio.timeout` for a
        deadline and in :func:`contextlib.aclosing` for deterministic cleanup.

        Args:
            backend_name (str or None): Backend name; active backend if ``None``.
            heartbeat (float): WebSocket ping interval in seconds.

        Returns:
            AsyncIterator[dict[str, Any]]: Async iterator of CloudEvent dicts (``type``, ``data`` with
                ``jobId`` and ``state``, ...).

        Raises:
            QException: Connection failed or the socket reported an error.

        Note:
            The websocket lives on the loop that owns the HTTP session; when
            iterated from another loop each step is forwarded there.
        """
        agen = self._iter_events_impl(backend_name, heartbeat)
        owner = self._session_loop
        if owner is None or owner is asyncio.get_running_loop():
            async with aclosing(agen):
                async for event in agen:
                    yield event
            return

        # run_coroutine_threadsafe() needs real coroutines, not async-gen awaitables.
        end = object()
        last_step: list[asyncio.Task] = []

        async def step():
            last_step[:] = [asyncio.current_task()]
            try:
                return await agen.__anext__()
            except StopAsyncIteration:
                return end

        async def close_gen():
            # A cancelled step may still be unwinding (closing the socket);
            # aclose() during that would fail with "already running".
            if last_step and not last_step[0].done():
                await asyncio.wait(last_step)
            await agen.aclose()

        try:
            while (event := await self._on_session_loop(step())) is not end:
                yield event
        finally:
            # Close the websocket on its own loop (no-op if already exhausted).
            try:
                await self._on_session_loop(close_gen())
            except QException as e:
                log.debug("Events websocket not closed cleanly: %s", e)

    async def _iter_events_impl(
        self, backend_name: str | None, heartbeat: float
    ) -> AsyncIterator[dict[str, Any]]:
        """Body of :meth:`iter_events`; must run on the session's loop."""
        session = self._require_session()
        ws_url = self._events_ws_url(backend_name)
        log.debug("Connecting events websocket %s", ws_url)
        try:
            ws = await session.ws_connect(
                ws_url,
                headers=self._auth_headers(),
                heartbeat=heartbeat,
                **self._ws_close_timeout(),
            )
        except aiohttp.ClientError as e:
            log.debug(
                "Events websocket %s connect failed: %s: %s",
                ws_url,
                type(e).__name__,
                e,
            )
            raise QException(f"Cannot connect to HEAppE events websocket: {e!s}") from e
        log.debug("Events websocket connected")
        received = 0
        try:
            async with ws:
                async for msg in ws:
                    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        raw = msg.data
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            log.warning("Received non-JSON event payload: %r", raw)
                            continue
                        received += 1
                        if log.isEnabledFor(logging.DEBUG):
                            # Defensive: events may not be dicts or may lack "data".
                            data = (
                                event.get("data") if isinstance(event, dict) else None
                            )
                            data = data if isinstance(data, dict) else {}
                            log.debug(
                                "Event #%d type=%s job=%s state=%s",
                                received,
                                event.get("type") if isinstance(event, dict) else None,
                                data.get("jobId"),
                                data.get("state"),
                            )
                        yield event
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise QException(f"Events websocket error: {ws.exception()!s}")
        finally:
            log.debug("Events websocket closed after %d event(s)", received)

    async def await_for_job_terminal_state(
        self, job_id: int, timeout: float | None = None, backend_name: str | None = None
    ) -> str:
        """
        Wait until a CloudEvent reports the job in a terminal state.

        Terminal states are listed in :attr:`TERMINAL_JOB_STATES`. Only events
        received after the websocket connects are seen, so a job that finished
        before the call is not detected; check :meth:`aget_job_info` first if
        that is possible.

        Args:
            job_id (int): HEAppE job ID.
            timeout (float or None): Maximum wait in seconds; unlimited if ``None``.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            str: Terminal state as reported by the event (original case).

        Raises:
            QResultsFailed: Timeout, or the event stream closed before a
                terminal state was reported.
            QException: WebSocket connection failed.
        """
        log.debug(
            "Waiting for job %s to reach a terminal state (timeout=%s)", job_id, timeout
        )
        # Timeout covers only waiting for events; the websocket close handshake (aclosing)
        # runs outside it, so a found state is never turned into a timeout during cleanup.
        async with aclosing(self.iter_events(backend_name=backend_name)) as events:
            try:
                async with asyncio.timeout(timeout):
                    async for event in events:
                        data = event.get("data") or {}
                        evt_job_id = data.get("jobId", None)
                        # Skip events of other jobs (job IDs may arrive as strings).
                        if evt_job_id is None or int(evt_job_id) != int(job_id):
                            continue
                        state = data.get("state")
                        log.debug("Job %s state: %s", job_id, state.upper())
                        if state.upper() in self.TERMINAL_JOB_STATES:
                            return state
            except TimeoutError:
                raise QResultsFailed(
                    job_id, f"Timed out waiting for terminal state after {timeout}s"
                ) from None
        raise QResultsFailed(
            job_id, "Event stream closed before job reached a terminal state."
        )

    def wait_for_job_terminal_state(
        self, job_id: int, timeout: float | None = None, backend_name: str | None = None
    ) -> str:
        """
        Sync wrapper of :meth:`await_for_job_terminal_state`.

        Args:
            job_id (int): HEAppE job ID.
            timeout (float or None): Maximum wait in seconds; unlimited if ``None``.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            str: Terminal state.

        Raises:
            QResultsFailed: Timeout or stream closed early.
            QException: Connection failed or called from the loop owning the HTTP session.
        """
        return self._run(
            self.await_for_job_terminal_state(
                job_id=job_id, timeout=timeout, backend_name=backend_name
            )
        )

    # =================================================================
    # Job lifecycle (JobManagement)
    # =================================================================

    async def cancel_job(self, job_id: int, backend_name: str | None = None) -> bool:
        """
        Cancel a job via ``PUT /heappe/JobManagement/CancelJob``.

        Errors are logged, not raised.

        Args:
            job_id (int): HEAppE job ID.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            bool: ``True`` if HEAppE accepted the request, ``False`` otherwise.
        """
        log.debug("Cancelling job %s", job_id)
        try:
            await self._request(
                "PUT",
                self._heappe_url("/heappe/JobManagement/CancelJob", backend_name),
                json_body={"SubmittedJobInfoId": job_id},
                timeout=30,
            )
            return True
        except QException as e:
            log.error("Failed to cancel job %s: %s", job_id, e)
            return False

    async def delete_job(
        self, job_id: int, archive_logs: bool = False, backend_name: str | None = None
    ) -> bool:
        """
        Delete a job and its data via ``DELETE /heappe/JobManagement/DeleteJob``.

        Errors are logged, not raised.

        Args:
            job_id (int): HEAppE job ID.
            archive_logs (bool): Keep the job logs archived on the HEAppE side.
            backend_name (str or None): Backend that runs the job; active backend if ``None``.

        Returns:
            bool: ``True`` if HEAppE accepted the request, ``False`` otherwise.
        """
        log.debug("Deleting job %s (archive_logs=%s)", job_id, archive_logs)
        try:
            await self._request(
                "DELETE",
                self._heappe_url("/heappe/JobManagement/DeleteJob", backend_name),
                json_body={"SubmittedJobInfoId": job_id, "ArchiveLogs": archive_logs},
                timeout=30,
            )
            return True
        except QException as e:
            log.error("Failed to delete job %s: %s", job_id, e)
            return False