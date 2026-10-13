"""Async QaaS client: LEXIS authentication + HEAppE QScheduler over aiohttp.

Usage:
    async with await QClient.create(token, "my_project", "my_resource") as client:
        md = await client.get_backend("VLQ")
        job = await client.submit_quantum_job(tasks, circuit_payloads=payloads)
        state = await client.wait_for_job_terminal_state(job["Id"], timeout=600)
        results = await client.get_job_results(job, artifact_names=["measurement_counts"])
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import itertools
import json
import logging
import os
import ssl
import sys
import time
import warnings
from collections.abc import AsyncIterator
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
from py4heappe.heappe_v6.core import models as heappe_models
from py4heappe.heappe_v6.core.api import (
    ClusterInformationApi,
    JobManagementApi
)
from py4heappe.heappe_v6.core.models import (
    ClusterExt,
)

from .backend_metadata import (
    LexisProject,
    LexisResource,
    QBackendMetadata,
    QCluster,
    QMachineInfo,
)
from .utils import QAuthException, QException, QResultsFailed

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
_BODY_PREVIEW_CHARS = 300
_request_ids = itertools.count(1)


def _preview(data: bytes | str, limit: int = _BODY_PREVIEW_CHARS) -> str:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    text = text.replace("\n", " ")
    return (
        text if len(text) <= limit else f"{text[:limit]}...(+{len(text) - limit} chars)"
    )


def _describe_body(json_body: Any, data: Any) -> str:
    """Short request body description for logs. Never includes payload file contents."""
    if json_body is not None:
        return f" json={_preview(json.dumps(json_body, default=str))}"
    if isinstance(data, aiohttp.MultipartWriter):
        return " body=multipart/form-data"
    if data is not None:
        return f" body={type(data).__name__}"
    return ""


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


def _to_utc(value: str) -> datetime:
    """Parse ISO date; keep an explicit offset, assume UTC only for naive values."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _g(obj: dict[str, Any], key: str) -> Any:
    """Read a HEAppE JSON field (PascalCase), tolerating camelCase serializers."""
    if key in obj:
        return obj[key]
    return obj.get(key[:1].lower() + key[1:])


_SWAGGER_PRIMITIVES = frozenset(
    {"int", "str", "bool", "float", "object", "date", "datetime"}
)


def _from_swagger_json(data: Any, type_name: str) -> Any:
    """Deserialize HEAppE JSON into py4heappe swagger models (e.g. 'list[ClusterExt]').

    Uses each model's swagger_types / attribute_map. Instances are created without
    calling __init__ so generated setters do not reject missing optional fields.
    Date/datetime values stay ISO strings.
    """
    if data is None:
        return None
    if type_name.startswith("list["):
        inner = type_name[5:-1]
        return [_from_swagger_json(item, inner) for item in data]
    if type_name.startswith("dict("):
        inner = type_name[type_name.index(",") + 1 : -1].strip()
        return {k: _from_swagger_json(v, inner) for k, v in data.items()}
    if type_name in _SWAGGER_PRIMITIVES:
        return data
    model_cls = getattr(heappe_models, type_name, None)
    if model_cls is None or not isinstance(data, dict):
        return data
    obj = model_cls.__new__(model_cls)
    obj.discriminator = None
    for attr, attr_type in model_cls.swagger_types.items():
        json_key = model_cls.attribute_map[attr]
        value = data[json_key] if json_key in data else _g(data, json_key)
        setattr(obj, f"_{attr}", _from_swagger_json(value, attr_type))
    return obj


def _query(params: dict[str, Any] | None) -> list[tuple[str, str | int]] | None:
    """aiohttp/yarl rejects None/bool/UUID/Enum query values; normalize them. Lists repeat the key."""
    if not params:
        return None
    out: list[tuple[str, str | int]] = []
    for key, value in params.items():
        if value is None:
            continue
        for item in value if isinstance(value, (list, tuple)) else (value,):
            if item is None:
                continue
            if isinstance(item, Enum):
                item = item.value
            if isinstance(item, bool):
                item = "true" if item else "false"
            elif not isinstance(item, (str, int)):
                item = str(item)
            out.append((key, item))
    return out


@dataclass(frozen=True)
class HttpResult:
    status: int
    content_type: str
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)

    def parsed(self) -> Any:
        """JSON if possible, raw bytes otherwise."""
        try:
            return self.json()
        except ValueError:
            return self.body


class MachineCalibrationIQMEndpoint(str, Enum):
    METRICS = "metrics"
    DYNAMIC_QUANTUM_ARCHITECTURE = "dynamic-quantum-architecture"


class QClient:
    """
    Async client for quantum backend communication with LEXIS token authentication via HEAppE.

    One QClient is bound to one LEXIS resource, which can contain several quantum
    clusters (assignments). Machine info for all clusters is loaded concurrently in
    `connect()`; the cluster to work with is selected by `get_backend(backend_name)`.
    Every job/session method also accepts an explicit `backend_name`.

    Create with `await QClient.create(...)` or `async with QClient(...) as client:`.
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
        """Store configuration only; no I/O. Call `await connect()` (or use create / async with)."""
        self._token = token
        self._lexis_project = lexis_project
        self._lexis_resource_name = lexis_resource_name
        self.provider_token = provider_token
        self._runner: asyncio.Runner | None = None
        self._load_machine_info_on_connect = load_machine_info
        self._kwargs = kwargs

        self._authenticated = False
        self._username: str | None = None
        self._lexis_project_info: dict[str, Any] = {}
        self._lexis_project_obj: LexisProject | None = None
        self._lexis_resource: LexisResource | None = None
        self._active_cluster: str | None = None

        # HTTP session: injected sessions are never closed by the client
        self._session = session
        self._owns_session = session is None
        self._is_view = False  # True for clients returned by bind()

        # HEAppE ProjectId cache per HEAppE URL (fallback lookup via accounting string)
        self._heappe_project_ids: dict[str, int] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
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
        """Construct and connect in one step."""
        client = cls(*args, **kwargs)
        try:
            await client._connect()
        except BaseException:
            await client.aclose()
            raise
        return client

    def _run(self, coro):
        """Run a coroutine on the client's own persistent loop (sync API)."""
        try:
            # Check if a loop is already running in this thread (e.g. Jupyter); if so, raise an error.
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop running in this thread -> OK
        else:
            coro.close()
            raise QException(
                "Sync QClient API called inside a running event loop (e.g. Jupyter); "
                "use the async methods with 'await' instead."
            )
        if self._runner is None:
            # Create a persistent asyncio.Runner for this client, so multiple calls can share the same loop.
            self._runner = asyncio.Runner()
            # Register atexit cleanup to close the runner and session when the program exits.
            atexit.register(self.close)
        return self._runner.run(coro)

    async def _connect(self) -> QClient:
        """Authenticate against LEXIS, discover clusters and load machine info."""
        if self._authenticated:
            return self
        # Create a new session if none exists or if the existing one is closed.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl_context()),
                timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT),
            )
            self._owns_session = True

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
        """Synchronous wrapper for `_connect()`."""
        try:
            return self._run(self._connect())
        except BaseException:
            self.close()  # don't leak the session on auth/connect errors
            raise

    async def aclose(self) -> None:
        """Close the HTTP session (if owned) and clear authentication state.

        Single place for cleanup logic: `async with` calls it via __aexit__,
        the sync `close()` runs it on the client's runner.

        On a view returned by bind() only the view is deactivated; the shared session
        stays open for the parent and other views.
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
            await self._session.close()
        self._session = None

    def close(self) -> None:
        """Sync wrapper of aclose(). The owning client also shuts down its event loop.

        Safe to call more than once. A bind() view never closes the shared runner.
        """
        runner = self._runner
        if runner is None:
            return  # sync API never used; async callers use `await aclose()`

        if self._is_view:
            coro = self.aclose()
            try:
                runner.run(coro)
            except RuntimeError:  # parent already closed the shared runner
                coro.close()
            return

        atexit.unregister(self.close)
        self._runner = None
        try:
            runner.run(self.aclose())  # session closed on the loop it was created in
        finally:
            runner.close()  # then the loop itself

    # Backwards-compatible name
    close_session = close

    async def __aenter__(self) -> Self:
        try:
            return await self._connect()
        except BaseException:
            await self.aclose()
            raise

    def __enter__(self) -> Self:
        return self.connect()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        if sys.platform == "win32":
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return ssl.create_default_context()

    # =================================================================
    # Low-level HTTP
    # =================================================================

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise QException("HTTP session is not open; call connect() first")
        return self._session

    def _auth_headers(self) -> dict[str, str]:
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
        """Single HTTP call. Body is read inside the context manager; HTTP >= 400 raises QException."""
        session = self._require_session()
        query = _query(params)
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
                    log.debug(
                        "[#%d] <- %s %s %d %s %dB %.0fms%s",
                        rid,
                        method,
                        resp.url.path,
                        resp.status,
                        resp.content_type,
                        len(body),
                        (time.perf_counter() - started) * 1000,
                        f" body={_preview(body)}"
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
        return f"{base.rstrip('/')}{path}"

    def _heappe_url(self, path: str, backend_name: str | None = None) -> str:
        return self._join(self._resolve_cluster(backend_name).heappe_url, path)

    async def _heappe_get(
        self,
        cluster: QCluster,
        path: str,
        params: dict[str, Any] | None = None,
        **kw: Any,
    ) -> HttpResult:
        return await self._request(
            "GET", self._join(cluster.heappe_url, path), params=params, **kw
        )

    # =================================================================
    # Authentication
    # =================================================================

    def _verify_backend_version(self, cluster: QCluster):
        def _parse_semver(v: str):
            v = (v or "").strip()
            if v.startswith(("v", "V")):
                v = v[1:]
            parts = v.split(".")
            if len(parts) < 3:
                raise ValueError(f"Invalid semantic version: {v!r}")
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
        """Verify the JWT signature against the issuer's JWKS (fetched with aiohttp)."""
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
        try:
            user_id = await self._verify_token()
        except QAuthException:
            raise
        except (jwt.InvalidTokenError, ValueError) as e:
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
                start_dt, end_dt = _to_utc(start), _to_utc(end)
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
        for spec in assignment.get("Specifications", []) or []:
            if spec.get("Key") == key:
                return spec.get("Value")
        return None

    def _is_quantum_assignment(self, assignment: dict[str, Any]) -> bool:
        return assignment.get("LocationTypeId") == self.DEFAULT_QUANTUM_LOCATION_TYPE

    async def _authorize_lexis_resource(
        self, lexis_resource_name: str | None = None
    ) -> LexisResource:
        """Authorize the LEXIS quantum resource and collect all its clusters (assignments).

        If `lexis_resource_name` is None, the project must contain exactly one quantum
        resource; otherwise an error listing the options is raised.
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
                in_range = _to_utc(start) <= datetime.now(UTC) <= _to_utc(end)
            except ValueError as date_error:
                log.warning("Could not parse resource dates: %s", date_error)
                in_range = True
            if not in_range:
                raise QAuthException(
                    reason=f"Resource '{resource_name}' is not within active dates ({start} - {end}).",
                    user_id=self._username,
                    resource=self._lexis_project,
                )

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
            project_resource_id=assignments[0].get("ProjectResourceId"),
            resource_validity_start=start,
            resource_validity_end=end,
            clusters=clusters,
        )

    # =================================================================
    # Cluster discovery, project ids & machine info
    # =================================================================

    def _matches_resource(self, project: Any) -> bool:
        """project: py4heappe project model from ClusterNodeTypeExt.projects."""
        name = self.lexis_resource.resource_name
        return name in (
            getattr(project, "name", None),
            getattr(project, "accounting_string", None),
        )

    async def _list_available_clusters(self, url: str) -> list[ClusterExt]:
        """Async equivalent of ClusterInformationApi.heappe_cluster_information_list_available_clusters_get."""
        res = await self._request(
            "GET", self._join(url, "/heappe/ClusterInformation/ListAvailableClusters")
        )
        return _from_swagger_json(res.json() or [], "list[ClusterExt]")

    async def _resolve_heappe_instance(self, url: str, wanted: list[QCluster]) -> None:
        """One ListAvailableClusters call for all clusters behind one HEAppE instance."""
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
        """Fill cluster_id / cluster_node_type_id / heappe_project_id for every cluster.

        HEAppE instances are queried concurrently. Failures are recorded on the cluster
        (available=False, error=...) instead of aborting connect().
        """
        by_url: dict[str, list[QCluster]] = {}
        for cluster in self.lexis_resource.clusters.values():
            by_url.setdefault(cluster.heappe_url, []).append(cluster)
        await asyncio.gather(
            *(self._resolve_heappe_instance(u, w) for u, w in by_url.items())
        )

    async def _project_id_for(self, cluster: QCluster) -> int:
        """HEAppE ProjectId of the cluster.

        Taken from ListAvailableClusters when present, otherwise looked up once per HEAppE
        instance via the accounting string (lock prevents duplicate concurrent lookups).
        """
        if cluster.heappe_project_id is not None:
            return cluster.heappe_project_id

        url = cluster.heappe_url
        lock = self._project_locks.setdefault(url, asyncio.Lock())
        async with lock:
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
                self._heappe_project_ids[url] = _g(projects[0], "Id")
        cluster.heappe_project_id = self._heappe_project_ids[url]
        return cluster.heappe_project_id

    async def _ids_for(self, backend_name: str | None) -> tuple[QCluster, int, int]:
        cluster = self._resolve_cluster(backend_name)
        if cluster.cluster_id is None:
            raise QException(
                f"HEAppE ClusterId not resolved for backend '{cluster.name}'."
            )
        return cluster, cluster.cluster_id, await self._project_id_for(cluster)

    async def _fetch_machine_info(self, cluster: QCluster) -> None:
        """Fetch /heappe/QScheduler/MachineInfo for one cluster and store it on the cluster.

        Never raises; the outcome is reflected in cluster.available / cluster.error.
        """
        if cluster.cluster_node_type_id is None:
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
        clusters = [
            c
            for c in self.lexis_resource.clusters.values()
            if c.cluster_node_type_id is not None
        ]
        await asyncio.gather(*(self._fetch_machine_info(c) for c in clusters))

    async def refresh_machine_info(self, backend_name: str | None = None) -> None:
        """Re-fetch machine info for one backend, or for all if backend_name is None."""
        if backend_name is None:
            await self._load_machine_info_all()
        else:
            await self._fetch_machine_info(self._resolve_cluster(backend_name))

    def _resolve_cluster(self, backend_name: str | None = None) -> QCluster:
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
        """Resolve a cluster and make sure it is usable. Does not change the active backend."""
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
        """Select a cluster (backend) and return its metadata.

        The selected backend becomes the default for methods called without backend_name.
        """
        cluster = await self._validate_backend(backend_name)
        self._active_cluster = cluster.name
        return await self._metadata(cluster, calibration_set_id)

    def bind(self, backend_name: str | None = None) -> QClient:
        """Return a client view pinned to one backend.

        Shallow copy sharing the HTTP session, LEXIS resource, clusters and caches, with its
        own active backend. aclose() on a view does not close the shared session.
        """
        cluster = self._run(self._validate_backend(backend_name))
        view = copy.copy(self)
        view._active_cluster = cluster.name
        view._is_view = True
        view._owns_session = False
        return view

    async def get_available_backends(
        self, with_architecture: bool = True
    ) -> dict[str, QBackendMetadata]:
        """All clusters of the resource (including unavailable ones, see .available/.error).

        with_architecture=False skips the per-backend architecture/calibration requests.
        """
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        items = sorted(self.lexis_resource.clusters.items())
        if with_architecture:
            mds = await asyncio.gather(*(self._metadata(c) for _, c in items))
        else:
            mds = [self._plain_metadata(c) for _, c in items]
        return dict(zip((n for n, _ in items), mds, strict=True))

    async def _get_quantum_backend_info(
        self,
        backend_name: str | None = None,
        calibration_set_id: UUID | str | None = None,
    ) -> QBackendMetadata:
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
        if not self.is_authenticated:
            raise QAuthException("Client not authenticated")
        return self._run(
            self._metadata(self._resolve_cluster(backend_name), calibration_set_id)
        )

    def _plain_metadata(self, cluster: QCluster) -> QBackendMetadata:
        return QBackendMetadata(self._lexis_project_obj, self.lexis_resource, cluster)

    async def _metadata(
        self, cluster: QCluster, calibration_set_id: UUID | str | None = None
    ) -> QBackendMetadata:
        """For IQM backends also fetch static and dynamic architecture (concurrently).

        Failures are logged and plain metadata is returned.
        """
        if not (cluster.machine_info and cluster.machine_info.sw_stack == "iqm"):
            return self._plain_metadata(cluster)
        log.debug(
            "Fetching architecture of '%s' (calibration_set_id=%s)",
            cluster.name,
            calibration_set_id or "default",
        )
        try:
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

    @staticmethod
    def _maybe_json(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError:
            return text

    # =================================================================
    # Properties
    # =================================================================

    @property
    def heappe_client(self) -> HEAppEApi | None:
        """Sync py4heappe client of the active backend (lazy). Not used by QClient itself."""
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
        """Sync py4heappe ClusterInformationApi of the active backend (blocking; not for async code)."""
        client = self.heappe_client
        return ClusterInformationApi(client) if client is not None else None

    @property
    def job_management_api(self) -> JobManagementApi | None:
        """Sync py4heappe JobManagementApi of the active backend (blocking; not for async code)."""
        client = self.heappe_client
        return JobManagementApi(client) if client is not None else None

    @property
    def session(self) -> aiohttp.ClientSession | None:
        return self._session

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._token is not None

    @property
    def lexis_project(self) -> str:
        return self._lexis_project

    @property
    def lexis_resource(self) -> LexisResource:
        if self._lexis_resource is None:
            raise QException("Client not connected; call connect() first")
        return self._lexis_resource

    @property
    def backend_names(self) -> list[str]:
        return self.lexis_resource.cluster_names

    @property
    def active_backend(self) -> str | None:
        return self._active_cluster

    @property
    def cluster_id(self) -> int | None:
        """HEAppE internal Cluster ID of the active backend."""
        return self._resolve_cluster().cluster_id if self._active_cluster else None

    @property
    def project_id(self) -> int | None:
        """HEAppE Project ID of the active backend, if already resolved (no I/O)."""
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
        Submit a job via POST /heappe/QScheduler/CreateAndSubmitJob (multipart/form-data).

        :param tasks: task specs, e.g.
            [{"Name": "GroverTask", "MachineId": "iqm_simulator",
              "WalltimeLimitSecs": 3600, "PayloadPartName": "circuit1",
              "UseSessions": False}]
        :param job_name: HEAppE job name
        :param circuit_payloads: maps a task's PayloadPartName -> file content to upload with the job
        :param backend_name: target cluster; defaults to the active backend
        :returns: SubmittedJobInfoExt as dict (Id, Name, State, Tasks[...])
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

    async def aget_job_info(self, job_id: int, backend_name: str | None = None) -> dict[str, Any]:
        """GET /heappe/JobManagement/CurrentInfoForJob -> SubmittedJobInfoExt (snake_case keys)."""
        log.debug("Fetching info of job %s", job_id)
        res = await self._request(
            "GET",
            self._heappe_url("/heappe/JobManagement/CurrentInfoForJob", backend_name),
            params={"SubmittedJobInfoId": int(job_id)},
        )
        return res.json()

    def get_job_info(self, job_id: int, backend_name: str | None = None) -> dict[str, Any]:
        """Sync wrapper of `aget_job_info()`."""
        return self._run(self.aget_job_info(job_id, backend_name))

    async def get_task_result(
        self, task_id: int, backend_name: str | None = None
    ) -> Any:
        """GET /heappe/QScheduler/GetTaskResult (IQM metadata, timeline, etc.)."""
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
        """GET /heappe/QScheduler/GetTaskArtifact (e.g. measurements, measurements_counts, sweep_results)."""
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
        """Fetch result (+ optional named artifacts) for every task of a job, concurrently."""
        sem = asyncio.Semaphore(max_concurrency)

        async def limited(coro):
            # Limits the number of concurrent executions of the coroutine using a semaphore.
            # This helps in controlling the load and preventing overwhelming the server.
            async with sem:
                return await coro

        async def one_task(task_id: int) -> tuple[int, dict[str, Any]]:
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
        cluster = cluster or self._resolve_cluster(backend_name)
        sw_stack = cluster.machine_info.sw_stack if cluster.machine_info else None
        if sw_stack == "iqm" and calibration_set_id is None:
            calibration_set_id = "default"
        res = await self._heappe_get(
            cluster,
            "/heappe/QScheduler/MachineCalibration",
            {
                "ClusterNodeTypeId": cluster_node_type_id
                or cluster.cluster_node_type_id,
                "CalibrationId": calibration_set_id,  # UUID/Enum normalized by _query
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
        """Calibration quality metrics of a backend as parsed JSON.

        :param backend_name: target backend; defaults to the active backend
        :param calibration_set_id: IQM calibration set; None -> current ("default")
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
                f"Metrics of backend '{cluster.name}' are not valid JSON: {_preview(raw, 200)}"
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
        """Sync wrapper of `machine_metrics()`."""
        return self._run(self.machine_metrics(backend_name, calibration_set_id))

    # =================================================================
    # QScheduler: sessions
    # =================================================================

    async def open_qscheduler_session(
        self, machine_id: str, walltime_limit_secs: int, backend_name: str | None = None
    ) -> int:
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
        base = self._resolve_cluster(backend_name).heappe_url.rstrip("/")
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/heappe/Events"

    @staticmethod
    def _ws_close_timeout(seconds: float = 5.0) -> dict[str, Any]:
        """Bound the close handshake (aiohttp >= 3.11: ClientWSTimeout, older: float)."""
        ws_timeout = getattr(aiohttp, "ClientWSTimeout", None)
        if ws_timeout is not None:
            return {"timeout": ws_timeout(ws_close=seconds)}
        return {"timeout": seconds}

    async def iter_events(
        self, backend_name: str | None = None, heartbeat: float = 30.0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded CloudEvents from the /heappe/Events websocket of the backend's HEAppE.

        Use asyncio.timeout(...) around the loop for an overall deadline.
        """
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
        """Wait until a CloudEvent reports job_id in a terminal state (FINISHED/FAILED/CANCELED)."""
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
        """Wait until a CloudEvent reports job_id in a terminal state (FINISHED/FAILED/CANCELED)."""
        return self._run(
            self.await_for_job_terminal_state(
                job_id=job_id, timeout=timeout, backend_name=backend_name
            )
        )

    # =================================================================
    # Job lifecycle (JobManagement)
    # =================================================================

    async def cancel_job(self, job_id: int, backend_name: str | None = None) -> bool:
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
