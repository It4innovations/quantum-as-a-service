"""Offline unit tests for qheappe_client: no network, no LEXIS token.

HTTP is replaced by a fake ``QClient._request`` that routes by URL path suffix, so the
LEXIS authorization, resource selection and HEAppE discovery logic run without I/O.
"""

import asyncio
import concurrent.futures
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import jwt
import pytest
import requests
import yarl
from aiohttp import web
from py4heappe.heappe_v6.core import models as heappe_models
from qiskit.providers.jobstatus import JobStatus

from qheappe_client.backend_metadata import (
    LexisProject,
    LexisResource,
    QBackendMetadata,
    QCluster,
    QMachineInfo,
)
from qheappe_client.qclient import (
    HttpResult,
    MachineCalibrationIQMEndpoint,
    QClient,
    _LoopThread,
)
from qheappe_client.qiskit.qbackend import (
    HEAppE_QISKIT_STATUS_MAPPING,
    QBackend,
    QJob,
    _HEAppEJobId,
)
from qheappe_client.qiskit.qbackend_iqm import QBackendIQM
from qheappe_client.qprovider import _split_target
from qheappe_client.utils import (
    HEAppEJobState,
    QAuthException,
    QException,
    QResultsFailed,
    from_swagger_json,
    is_valid_uuid,
    normalize_query,
    req_body_preview,
    req_describe_body,
    to_utc,
)

# ---------- fake HTTP ----------


def http(payload, status=200) -> HttpResult:
    return HttpResult(status, "application/json", json.dumps(payload).encode())


def fake_routes(client: QClient, routes: dict) -> list[tuple[str, str]]:
    """Replace client._request; route by URL path suffix. Returns the call log."""
    calls: list[tuple[str, str]] = []

    async def fake_request(method, url, **kw):
        calls.append((method, url))
        path = yarl.URL(url).path
        for suffix, resp in routes.items():
            if path.endswith(suffix):
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        raise AssertionError(f"unexpected request {method} {url}")

    client._request = fake_request
    return calls


def run(coro):
    return asyncio.run(coro)


# ---------- utils: query / body helpers ----------


class TestNormalizeQuery:
    def test_empty_inputs(self):
        assert normalize_query(None) == []
        assert normalize_query({}) == []
        assert normalize_query({"a": None}) == []

    def test_value_conversions(self):
        u = uuid4()
        out = normalize_query({"b": True, "f": False, "u": u, "i": 3, "s": "x"})
        assert out == [
            ("b", "true"),
            ("f", "false"),
            ("u", str(u)),
            ("i", 3),
            ("s", "x"),
        ]

    def test_list_repeats_key_and_skips_none(self):
        assert normalize_query({"k": ["x", None, "y"], "t": (1, 2)}) == [
            ("k", "x"),
            ("k", "y"),
            ("t", 1),
            ("t", 2),
        ]

    def test_str_enum_is_sent_as_value(self):
        # MachineCalibration passes the endpoint enum straight through.
        q = normalize_query({"Endpoint": MachineCalibrationIQMEndpoint.METRICS})
        assert yarl.URL("https://h/x").with_query(q).query_string == "Endpoint=metrics"


def test_req_body_preview():
    assert req_body_preview(b"a\nb") == "a b"
    p = req_body_preview("x" * 10, limit=4)
    assert p.startswith("xxxx") and "(+6 chars)" in p


def test_describe_body():
    assert req_describe_body({"a": 1}, None) == ' json={"a": 1}'
    assert (
        req_describe_body(None, aiohttp.MultipartWriter("form-data"))
        == " body=multipart/form-data"
    )
    assert req_describe_body(None, b"raw") == " body=bytes"
    assert req_describe_body(None, None) == ""


class TestToUtc:
    def test_naive_is_utc(self):
        assert to_utc("2026-01-01T00:00:00").utcoffset().total_seconds() == 0

    def test_offset_kept(self):
        assert to_utc("2026-01-01T00:00:00+02:00").utcoffset().total_seconds() == 7200

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            to_utc("not a date")


# ---------- utils: swagger deserialization ----------


def full(model: str, **values):
    """JSON object for a py4heappe model with every attribute present (None by default)."""
    cls = getattr(heappe_models, model)
    obj = {json_key: None for json_key in cls.attribute_map.values()}
    obj.update(values)
    return obj


class TestFromSwaggerJson:
    def test_primitives_and_none(self):
        assert from_swagger_json(None, "ClusterExt") is None
        assert from_swagger_json(5, "int") == 5
        assert from_swagger_json("2026-01-01", "datetime") == "2026-01-01"

    def test_containers(self):
        assert from_swagger_json([1, 2], "list[int]") == [1, 2]
        assert from_swagger_json({"a": 1}, "dict(str, int)") == {"a": 1}

    def test_unknown_type_passthrough(self):
        assert from_swagger_json({"x": 1}, "NoSuchModel") == {"x": 1}

    def test_nested_model(self):
        data = [
            full(
                "ClusterExt",
                Id=1,
                Name="VLQ",
                NodeTypes=[full("ClusterNodeTypeExt", Id=5, Name="qpu")],
            )
        ]
        (cluster,) = from_swagger_json(data, "list[ClusterExt]")
        assert isinstance(cluster, heappe_models.ClusterExt)
        assert (cluster.id, cluster.name) == (1, "VLQ")
        assert cluster.node_types[0].id == 5

    @pytest.mark.xfail(
        strict=True,
        raises=KeyError,
        reason="strict data[json_key]: a field missing in the response "
        "(py4heappe/HEAppE version skew) raises KeyError",
    )
    def test_missing_field_tolerated(self):
        data = full("ClusterExt", Id=1, Name="VLQ", NodeTypes=[])
        data.pop("Description")
        assert from_swagger_json(data, "ClusterExt").id == 1


# ---------- utils: states / exceptions ----------


class TestHEAppEJobState:
    @pytest.mark.parametrize(
        ("value", "name"),
        [
            (1, "CONFIGURING"),
            (2, "SUBMITTED"),
            (4, "QUEUED"),
            (8, "RUNNING"),
            (16, "FINISHED"),
            (32, "FAILED"),
            (64, "CANCELED"),
        ],
    )
    def test_readable(self, value, name):
        assert HEAppEJobState.readable(value) == name

    def test_unknown(self):
        assert HEAppEJobState.readable(999) == "Unknown state 999"


def test_is_valid_uuid():
    u = uuid4()
    assert is_valid_uuid(u)
    assert is_valid_uuid(str(u))
    assert not is_valid_uuid("default")


class TestExceptions:
    def test_qexception_context_in_message(self):
        assert str(QException("boom", resource="R")) == "boom (resource=R)"
        assert str(QException()) == "QException"

    def test_auth_from_expired_jwt(self):
        e = QAuthException.from_cause(jwt.ExpiredSignatureError(), resource="P")
        assert "expired" in e.reason and e.resource == "P"
        assert isinstance(e, QException)

    def test_auth_from_http_401(self):
        err = requests.HTTPError(response=SimpleNamespace(status_code=401, text=""))
        assert "not authorized" in QAuthException.from_cause(err).reason

    def test_auth_from_other_exception_keeps_text(self):
        assert "boom" in QAuthException.from_cause(QException("boom")).reason

    def test_auth_rewrap_keeps_details(self):
        inner = QAuthException("no access", user_id="u", resource="r")
        outer = QAuthException(cause=inner)
        assert (outer.reason, outer.user_id, outer.resource) == ("no access", "u", "r")

    def test_results_failed_message(self):
        assert "HEAppE job '7'" in str(QResultsFailed(7, "x"))


# ---------- QClient without I/O ----------


def test_http_result_parsed():
    assert http({"a": 1}).parsed() == {"a": 1}
    assert (
        HttpResult(200, "application/octet-stream", b"\x00raw").parsed() == b"\x00raw"
    )


def test_spec_value():
    a = {"Specifications": [{"Key": "HEAPPE_URL", "Value": "https://h"}]}
    assert QClient._spec_value(a, "HEAPPE_URL") == "https://h"
    assert QClient._spec_value({"Specifications": None}, "HEAPPE_URL") is None


class TestQClientLifecycle:
    def test_construction_does_no_io(self):
        c = QClient("tok", "proj", lexis_api_url="https://api.example/")
        assert not c.is_authenticated
        assert c.active_backend is None
        assert c._lexis_userorg_api_url == "https://api.example/userorg"

    def test_unconnected_access_raises(self):
        c = QClient("tok", "proj")
        with pytest.raises(QException):
            _ = c.lexis_resource
        with pytest.raises(QException):
            c._require_session()

    def test_close_is_idempotent(self):
        c = QClient("tok", "proj")
        c.close()
        c.close()


class TestTokenVerification:
    def test_symmetric_alg_rejected_before_any_request(self):
        token = jwt.encode(
            {"iss": "https://issuer.example"}, "secret", algorithm="HS256"
        )
        c = QClient(token, "proj")
        calls = fake_routes(c, {})
        with pytest.raises(ValueError, match="Unsupported JWT algorithm"):
            run(c._verify_token())
        assert calls == []

    def test_missing_issuer_rejected(self):
        c = QClient(jwt.encode({"sub": "u"}, "secret", algorithm="HS256"), "proj")
        with pytest.raises(ValueError, match="issuer"):
            run(c._verify_token())

    def test_garbage_token_maps_to_auth_exception(self):
        c = QClient("not-a-jwt", "proj")
        with pytest.raises(QAuthException, match="decode"):
            run(c._authenticate_authorize_lexis())


# ---------- LEXIS project authorization ----------


def authed_client(projects) -> QClient:
    c = QClient("tok", "proj")

    async def verified():
        return "user"

    c._verify_token = verified
    fake_routes(c, {"/userorg/api/Project": http(projects)})
    return c


class TestProjectAuthorization:
    def test_project_without_dates(self):
        user, info = run(
            authed_client([{"ShortName": "proj"}])._authenticate_authorize_lexis()
        )
        assert user == "user" and info["ShortName"] == "proj"

    def test_filter_is_not_trusted(self):
        c = authed_client([{"ShortName": "other"}, {"ShortName": "x"}])
        with pytest.raises(
            QAuthException, match=r"does not have access.*\['other', 'x'\]"
        ):
            run(c._authenticate_authorize_lexis())

    def test_active_project(self):
        c = authed_client(
            [
                {
                    "ShortName": "proj",
                    "StartDate": "2000-01-01T00:00:00",
                    "EndDate": "2999-01-01T00:00:00",
                }
            ]
        )
        assert run(c._authenticate_authorize_lexis())[0] == "user"

    def test_expired_project(self):
        c = authed_client(
            [{"ShortName": "proj", "StartDate": "2000-01-01", "EndDate": "2001-01-01"}]
        )
        with pytest.raises(QAuthException, match="expired"):
            run(c._authenticate_authorize_lexis())

    def test_not_started_project(self):
        c = authed_client(
            [{"ShortName": "proj", "StartDate": "2998-01-01", "EndDate": "2999-01-01"}]
        )
        with pytest.raises(QAuthException, match="not started"):
            run(c._authenticate_authorize_lexis())

    def test_unparsable_dates(self):
        c = authed_client([{"ShortName": "proj", "StartDate": "x", "EndDate": "y"}])
        with pytest.raises(QAuthException, match="parse"):
            run(c._authenticate_authorize_lexis())

    def test_userorg_failure(self):
        c = QClient("tok", "proj")

        async def verified():
            return "user"

        c._verify_token = verified
        fake_routes(c, {"/userorg/api/Project": QException("HTTP 503")})
        with pytest.raises(QAuthException, match="503"):
            run(c._authenticate_authorize_lexis())


# ---------- LEXIS resource selection ----------


def assignment(name, url="https://h1", loc=7, **extra):
    a = {
        "LocationTypeId": loc,
        "LocationName": name,
        "ProjectResourceId": "pr-1",
        "AllocationAmount": 10,
        "Specifications": [{"Key": "HEAPPE_URL", "Value": url}],
    }
    a.update(extra)
    return a


def resource(name, *assignments, **extra):
    return {"Name": name, "Assignments": list(assignments), **extra}


def resource_client(resources) -> QClient:
    c = QClient("tok", "proj")
    c._authenticated, c._username = True, "user"
    fake_routes(c, {"/userorg/api/ProjectResource": http(resources)})
    return c


class TestResourceSelection:
    def test_single_quantum_resource_auto_selected(self):
        c = resource_client(
            [
                resource("HPC", assignment("Karolina", loc=1)),
                resource(
                    "Q",
                    assignment("VLQ"),
                    assignment("MOCK", url="https://h2"),
                    assignment("IGNORED", loc=1),
                ),
            ]
        )
        r = run(c._authorize_lexis_resource(None))
        assert r.resource_name == "Q"
        assert r.cluster_names == ["MOCK", "VLQ"]
        assert r.clusters["MOCK"].heappe_url == "https://h2"
        assert r.allocated_units == 20 and r.project_resource_id == "pr-1"

    def test_named_resource(self):
        c = resource_client(
            [resource("Q1", assignment("A")), resource("Q2", assignment("B"))]
        )
        assert run(c._authorize_lexis_resource("Q2")).cluster_names == ["B"]

    def test_ambiguous_without_name(self):
        c = resource_client(
            [resource("Q1", assignment("A")), resource("Q2", assignment("B"))]
        )
        with pytest.raises(QAuthException, match="Multiple"):
            run(c._authorize_lexis_resource(None))

    def test_unknown_name(self):
        c = resource_client([resource("Q1", assignment("A"))])
        with pytest.raises(QAuthException, match=r"not found.*\['Q1'\]"):
            run(c._authorize_lexis_resource("NOPE"))

    def test_no_quantum_resource(self):
        c = resource_client([resource("HPC", assignment("Karolina", loc=1))])
        with pytest.raises(QAuthException, match="No quantum resource"):
            run(c._authorize_lexis_resource(None))

    def test_incomplete_and_duplicate_assignments(self):
        bad = assignment("NOURL")
        bad["Specifications"] = []
        c = resource_client(
            [
                resource(
                    "Q",
                    bad,
                    assignment("VLQ", url="https://first"),
                    assignment("VLQ", url="https://second"),
                )
            ]
        )
        r = run(c._authorize_lexis_resource(None))
        assert r.cluster_names == ["VLQ"]
        assert r.clusters["VLQ"].heappe_url == "https://first"

    def test_no_usable_assignment(self):
        bad = assignment("NOURL")
        bad["Specifications"] = []
        c = resource_client([resource("Q", bad)])
        with pytest.raises(QAuthException, match="no usable"):
            run(c._authorize_lexis_resource(None))

    def test_resource_outside_dates(self):
        c = resource_client(
            [
                resource(
                    "Q", assignment("A"), StartDate="2000-01-01", EndDate="2001-01-01"
                )
            ]
        )
        with pytest.raises(QAuthException, match="active dates"):
            run(c._authorize_lexis_resource(None))

    def test_unparsable_resource_dates_are_lenient(self):
        c = resource_client(
            [resource("Q", assignment("A"), StartDate="x", EndDate="y")]
        )
        assert run(c._authorize_lexis_resource(None)).resource_name == "Q"

    def test_requires_authentication(self):
        with pytest.raises(QAuthException):
            run(QClient("tok", "proj")._authorize_lexis_resource(None))

    def test_request_failure_maps_to_auth_exception(self):
        c = QClient("tok", "proj")
        c._authenticated = True
        fake_routes(c, {"/userorg/api/ProjectResource": QException("HTTP 503")})
        with pytest.raises(QAuthException, match="Failed to authorize LEXIS resource"):
            run(c._authorize_lexis_resource(None))


# ---------- HEAppE discovery, project id, machine info ----------


def connected_client(*clusters: QCluster, resource_name="Q") -> QClient:
    c = QClient("tok", "proj")
    c._authenticated = True
    c._lexis_resource = LexisResource(
        resource_name, "pr", "-", "-", {cl.name: cl for cl in clusters}
    )
    return c


def cluster(name="VLQ", url="https://h1", **kw) -> QCluster:
    return QCluster(name=name, heappe_url=url, **kw)


def heappe_cluster(name, cid, node_type_id):
    return full(
        "ClusterExt",
        Id=cid,
        Name=name,
        NodeTypes=[full("ClusterNodeTypeExt", Id=node_type_id, Name="qpu")],
    )


class TestHEAppEDiscovery:
    def test_ids_resolved(self):
        vlq = cluster()
        c = connected_client(vlq)
        fake_routes(c, {"/ListAvailableClusters": http([heappe_cluster("VLQ", 3, 7)])})
        run(c._resolve_heappe_clusters())
        assert (vlq.cluster_id, vlq.cluster_node_type_id, vlq.error) == (3, 7, None)

    def test_one_request_per_heappe_instance(self):
        a, b, other = cluster("A"), cluster("B"), cluster("C", url="https://h2")
        c = connected_client(a, b, other)
        calls = fake_routes(
            c,
            {
                "/ListAvailableClusters": http(
                    [
                        heappe_cluster("A", 1, 10),
                        heappe_cluster("B", 2, 20),
                        heappe_cluster("C", 3, 30),
                    ]
                )
            },
        )
        run(c._resolve_heappe_clusters())
        assert len(calls) == 2
        assert [x.cluster_node_type_id for x in (a, b, other)] == [10, 20, 30]

    def test_cluster_not_exposed(self):
        vlq = cluster()
        c = connected_client(vlq)
        fake_routes(
            c, {"/ListAvailableClusters": http([heappe_cluster("OTHER", 1, 1)])}
        )
        run(c._resolve_heappe_clusters())
        assert vlq.cluster_id is None and "not exposed" in vlq.error

    def test_unreachable_instance_marks_unavailable(self):
        vlq = cluster(available=True)
        c = connected_client(vlq)
        fake_routes(c, {"/ListAvailableClusters": QException("HTTP 502")})
        run(c._resolve_heappe_clusters())
        assert not vlq.available and "ListAvailableClusters failed" in vlq.error


class TestProjectIdLookup:
    def test_preset_project_id_needs_no_request(self):
        c = connected_client(cluster(heappe_project_id=9))
        calls = fake_routes(c, {})
        assert run(c._project_id_for(c.lexis_resource.clusters["VLQ"])) == 9
        assert calls == []

    def test_lookup_cached_per_instance(self):
        a, b = cluster("A"), cluster("B")
        c = connected_client(a, b)
        calls = fake_routes(c, {"/ProjectsByAccountingStrings": http([{"Id": 42}])})

        async def both():
            return await asyncio.gather(c._project_id_for(a), c._project_id_for(b))

        assert run(both()) == [42, 42]
        assert len(calls) == 1

    def test_not_found(self):
        c = connected_client(cluster())
        fake_routes(c, {"/ProjectsByAccountingStrings": http([])})
        with pytest.raises(QException, match="not found"):
            run(c._project_id_for(c.lexis_resource.clusters["VLQ"]))


class TestMachineInfo:
    def _fetch(self, payload):
        vlq = cluster(cluster_id=1, cluster_node_type_id=2, heappe_project_id=3)
        c = connected_client(vlq)
        fake_routes(c, {"/QScheduler/MachineInfo": payload})
        run(c._fetch_machine_info(vlq))
        return vlq

    def test_supported_stack(self):
        vlq = self._fetch(
            http({"type": "iqm", "version": {"qscheduler_version": "1.0.0"}})
        )
        assert vlq.available and vlq.error is None
        assert vlq.machine_info.supplier == "IQM"

    def test_unsupported_stack(self):
        vlq = self._fetch(
            http({"type": "other", "version": {"qscheduler_version": "1.0.0"}})
        )
        assert not vlq.available and "unsupported" in vlq.error

    def test_incomplete_info(self):
        vlq = self._fetch(http({"type": "iqm"}))
        assert not vlq.available and "incomplete" in vlq.error

    def test_request_failure(self):
        vlq = self._fetch(QException("HTTP 500"))
        assert not vlq.available and "MachineInfo failed" in vlq.error

    def test_validate_backend_reports_error(self):
        vlq = cluster()  # no node type -> unavailable
        c = connected_client(vlq)
        with pytest.raises(QException, match="not available"):
            run(c._validate_backend("VLQ"))

    def test_unknown_backend(self):
        c = connected_client(cluster())
        with pytest.raises(QException, match=r"Unknown backend 'X'.*\['VLQ'\]"):
            c._resolve_cluster("X")


# ---------- sync API on a background loop thread (Jupyter-safe) ----------


def in_running_loop(cell):
    """Run ``cell`` (an async function) like a Jupyter cell: a loop runs in this thread."""
    return asyncio.run(cell())


class TestLoopThread:
    def test_runs_on_its_own_thread(self):
        lt = _LoopThread()
        try:

            async def where():
                return threading.get_ident(), asyncio.get_running_loop()

            tid, loop = lt.run(where())
            assert tid == lt.thread.ident != threading.get_ident()
            assert loop is lt.loop
        finally:
            lt.stop()

    def test_exception_propagates(self):
        lt = _LoopThread()

        async def boom():
            raise QException("boom")

        try:
            with pytest.raises(QException, match="boom"):
                lt.run(boom())
            assert lt.run(asyncio.sleep(0, "still alive")) == "still alive"
        finally:
            lt.stop()

    def test_call_from_loop_thread_itself_raises(self):
        lt = _LoopThread()

        async def nested():
            with pytest.raises(QException, match="deadlock"):
                lt.run(asyncio.sleep(0))
            return True

        try:
            assert lt.run(nested())
        finally:
            lt.stop()

    def test_run_after_stop_raises(self):
        lt = _LoopThread()
        lt.stop()
        lt.stop()  # idempotent
        assert not lt.alive
        with pytest.raises(QException, match="closed"):
            lt.run(asyncio.sleep(0))

    def test_interrupt_cancels_coroutine(self, monkeypatch):
        lt = _LoopThread()
        started, cancelled = threading.Event(), threading.Event()

        async def long_request():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def interrupted_wait(fs, timeout=None):
            started.wait(5)
            raise KeyboardInterrupt  # Ctrl-C while the caller blocks

        monkeypatch.setattr(concurrent.futures, "wait", interrupted_wait)
        try:
            with pytest.raises(KeyboardInterrupt):
                lt.run(long_request())
            assert cancelled.wait(5), "coroutine was not cancelled on the loop thread"
        finally:
            lt.stop()


class TestSyncApiInRunningLoop:
    def test_sync_call_inside_running_loop_works(self):
        c = QClient("tok", "proj")

        async def cell():
            return c._run(asyncio.sleep(0, "ok"))

        try:
            assert in_running_loop(cell) == "ok"
        finally:
            c.close()

    def test_loop_thread_reused_and_stopped_on_close(self):
        c = QClient("tok", "proj")
        c._run(asyncio.sleep(0))
        lt = c._loop_thread
        c._run(asyncio.sleep(0))
        assert c._loop_thread is lt
        c.close()
        assert c._loop_thread is None and not lt.alive
        c.close()

    def test_sync_call_from_session_owner_loop_raises(self):
        c = QClient("tok", "proj")

        async def cell():
            c._session_loop = asyncio.get_running_loop()  # as after `async with c:`
            with pytest.raises(QException, match="owns this client's HTTP session"):
                c._run(asyncio.sleep(0))

        in_running_loop(cell)
        assert c._loop_thread is None  # rejected before starting a thread

    def test_on_session_loop_forwards_to_owner(self):
        owner = _LoopThread()
        c = QClient("tok", "proj")
        c._session_loop = owner.loop

        async def where():
            return threading.get_ident()

        async def cell():
            return await c._on_session_loop(where())

        try:
            assert in_running_loop(cell) == owner.thread.ident
        finally:
            owner.stop()
        with pytest.raises(QException, match="no longer running"):
            in_running_loop(cell)


# ---------- end to end against a local aiohttp server ----------


@pytest.fixture
def heappe_server():
    """Fake HEAppE (job info + events websocket) on its own loop thread."""
    state = {"events": [], "hang": False, "ws_closed": threading.Event()}

    async def job_info(request):
        return web.json_response({"Id": int(request.query["SubmittedJobInfoId"])})

    async def events(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            for event in state["events"]:
                await ws.send_json(event)
            if state["hang"]:
                async for _ in ws:  # keep open until the client closes
                    pass
        finally:
            state["ws_closed"].set()
        return ws

    async def start():
        app = web.Application()
        app.router.add_get("/heappe/JobManagement/CurrentInfoForJob", job_info)
        app.router.add_get("/heappe/Events", events)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, runner.addresses[0][1]

    server = _LoopThread("fake-heappe")
    runner, port = server.run(start())
    state["url"] = f"http://127.0.0.1:{port}"
    try:
        yield state
    finally:
        server.run(runner.cleanup())
        server.stop()


def offline_client(url: str) -> QClient:
    """Client whose LEXIS/HEAppE discovery is stubbed; HTTP goes to ``url`` for real."""
    c = QClient("tok", "proj", load_machine_info=False)
    vlq = cluster(
        url=url,
        cluster_id=1,
        cluster_node_type_id=2,
        heappe_project_id=3,
        machine_info=QMachineInfo.from_response(
            {"type": "iqm", "version": {"qscheduler_version": "1.0.0"}}
        ),
        available=True,
    )

    async def authenticate():
        return "user", {"ShortName": "proj"}

    async def authorize(_name):
        return LexisResource("Q", "pr", "-", "-", {"VLQ": vlq})

    async def noop():
        return None

    c._authenticate_authorize_lexis = authenticate
    c._authorize_lexis_resource = authorize
    c._resolve_heappe_clusters = noop
    return c


class TestSyncAndAsyncAcrossLoops:
    def test_jupyter_sync_and_await_on_one_client(self, heappe_server):
        c = offline_client(heappe_server["url"])

        async def cell():
            c.connect()  # sync connect inside a running loop
            assert c._session_loop is c._loop_thread.loop
            sync_info = c.get_job_info(5)
            async_info = await c.aget_job_info(6)  # forwarded to the session's loop
            return sync_info, async_info

        try:
            assert in_running_loop(cell) == ({"Id": 5}, {"Id": 6})
            session = c.session
        finally:
            c.close()
        assert session.closed and c.session is None

    def test_async_connected_sync_call_on_same_loop_raises(self, heappe_server):
        c = offline_client(heappe_server["url"])

        async def cell():
            async with c:
                assert await c.aget_job_info(1) == {"Id": 1}
                with pytest.raises(QException, match="use 'await'"):
                    c.get_job_info(1)

        in_running_loop(cell)
        assert c.session is None

    def test_async_connected_used_sync_from_other_thread(self, heappe_server):
        app_loop = _LoopThread("app")  # e.g. an async framework's loop
        c = offline_client(heappe_server["url"])
        try:
            app_loop.run(c._connect())
            session = c.session
            assert c.get_job_info(3) == {
                "Id": 3
            }  # client loop -> forwarded to app loop
            c.close()  # session closed on the app loop
            assert session.closed
        finally:
            app_loop.stop()

    def test_bind_view_inside_running_loop(self, heappe_server):
        c = offline_client(heappe_server["url"])

        async def cell():
            c.connect()
            view = c.bind("VLQ")
            assert view.get_job_info(2) == {"Id": 2}
            view.close()
            assert c.get_job_info(3) == {"Id": 3}  # parent unaffected
            with pytest.raises(QException, match="view is closed"):
                view.get_job_info(4)

        try:
            in_running_loop(cell)
        finally:
            c.close()

    def test_events_across_loops(self, heappe_server):
        heappe_server["events"] = [
            {"data": {"jobId": "8", "state": "Finished"}},  # other job
            {"data": {"jobId": "7", "state": "Running"}},
            {"data": {"jobId": 7, "state": "Finished"}},
        ]
        c = offline_client(heappe_server["url"])

        async def cell():
            c.connect()
            awaited = await c.await_for_job_terminal_state(7, timeout=5)
            synced = c.wait_for_job_terminal_state(7, timeout=5)
            return awaited, synced

        try:
            assert in_running_loop(cell) == ("Finished", "Finished")
        finally:
            c.close()

    @pytest.mark.parametrize("mode", ["await", "sync"])
    def test_events_timeout_closes_websocket(self, heappe_server, mode):
        heappe_server["hang"] = True
        c = offline_client(heappe_server["url"])

        async def cell():
            c.connect()
            with pytest.raises(QResultsFailed, match="Timed out"):
                if mode == "await":
                    await c.await_for_job_terminal_state(7, timeout=0.3)
                else:
                    c.wait_for_job_terminal_state(7, timeout=0.3)
            # client still usable afterwards
            assert c.get_job_info(1) == {"Id": 1}

        try:
            in_running_loop(cell)
            assert heappe_server["ws_closed"].wait(5), "websocket left open"
        finally:
            c.close()


# ---------- metadata ----------


class TestMetadata:
    def test_machine_info_iqm(self):
        mi = QMachineInfo.from_response(
            {"type": "iqm", "version": {"qscheduler_version": "1.2.3"}}
        )
        assert (mi.supplier, mi.quantum_technology, mi.qscheduler_version) == (
            "IQM",
            "SUPERCONDUCTING",
            "1.2.3",
        )

    def test_machine_info_unknown_stack(self):
        mi = QMachineInfo.from_response({"type": "other"})
        assert mi.supplier == "UNKNOWN" and mi.qscheduler_version is None

    def test_resource_clusters(self):
        r = LexisResource(
            "R",
            "id",
            "-",
            "-",
            {
                "B": cluster(name="B", allocated_units=5),
                "A": cluster(name="A"),
            },
        )
        assert r.cluster_names == ["A", "B"]
        assert r.allocated_units == 5
        with pytest.raises(KeyError, match="Available"):
            r.get_cluster("X")

    def test_backend_metadata_without_machine_info(self):
        md = QBackendMetadata(
            LexisProject("P"), LexisResource("R", "id", "-", "-"), cluster()
        )
        assert md.backend_name == "VLQ"
        assert md.software_stack is None and md.supplier == "-" and not md.available

    def test_split_target(self):
        md = QBackendMetadata(
            LexisProject("P"), LexisResource("R", "id", "-", "-"), cluster()
        )
        assert _split_target("R", "VLQ") == ("R", "VLQ")
        assert _split_target(md, None) == ("R", "VLQ")
        assert _split_target(md, "VLQ") == ("R", "VLQ")
        with pytest.raises(ValueError, match="conflicts"):
            _split_target(md, "OTHER")


# ---------- job ids / status mapping ----------


def test_heappe_job_id_is_int_and_callable():
    jid = _HEAppEJobId(66)
    assert jid == 66 and jid() == "66"


def test_every_heappe_state_is_mapped():
    missing = {s.name.upper() for s in HEAppEJobState} - set(
        HEAppE_QISKIT_STATUS_MAPPING
    )
    assert not missing, missing


# ---------- result conversion ----------
# Raw artifact formats as consumed by QJob.result():
#   measurement_counts: [{"measurement_keys": [...], "counts": {"01": n, ...}}]
#   measurements:       [{key: [[bit, ...], ...shots]}]

RAW = [{"measurement_keys": ["m"], "counts": {"01": 2, "10": 1}}]
MEAS = [{"m": [[0, 1], [0, 1], [1, 0]]}]


def _expected():
    """Non-IQM keys are read as given (most significant bit first)."""
    return dict(RAW[0]["counts"]), ["01", "01", "10"]


class TestResultConversion:
    def test_primitive_counts_path(self):
        res = QBackendIQM.result_to_qiskit(None, RAW)
        assert res[0].data.meas.get_counts() == _expected()[0]
        assert res[0].metadata["measurement_keys"] == ["m"]

    def test_primitive_measurement_path_agrees(self):
        res = QBackendIQM.result_to_qiskit(None, RAW, measurements_raw=MEAS)
        counts, memory = _expected()
        assert res[0].data.meas.get_counts() == counts
        assert res[0].data.meas.get_bitstrings() == memory

    def test_legacy(self):
        res = QBackendIQM.result_to_qiskit_legacy(
            None, RAW, measurements_raw=MEAS, backend_name="b", job_id="1"
        )
        counts, memory = _expected()
        assert res.get_counts() == counts
        assert res.get_memory() == memory


class TestResultConversionErrors:
    def test_measurement_set_count_mismatch(self):
        with pytest.raises(ValueError, match="measurement sets"):
            QBackendIQM.result_to_qiskit(None, RAW, measurements_raw=MEAS * 2)

    def test_shots_mismatch(self):
        with pytest.raises(ValueError, match="shots"):
            QBackendIQM.result_to_qiskit(None, RAW, measurements_raw=[{"m": [[0, 1]]}])

    def test_keys_with_different_shot_counts(self):
        raw = [{"measurement_keys": ["a", "b"], "counts": {"00": 1}}]
        with pytest.raises(ValueError, match="different shot counts"):
            QBackendIQM.result_to_qiskit(
                None, raw, measurements_raw=[{"a": [[0]], "b": [[0], [1]]}]
            )


# --- run(memory=...) -> per-shot data in QJob.result() (issue #49) ---


class _FakeJobClient:
    """QClient stand-in: a finished job whose artifacts include per-shot data."""

    TERMINAL_JOB_STATES = QClient.TERMINAL_JOB_STATES

    def __init__(self):
        self.requested_artifacts = []

    def get_quantum_backend_info(self):
        return SimpleNamespace(backend_name="VLQ")

    async def submit_quantum_job(self, **kwargs):
        return {"Id": 7}

    def _run(self, coro):
        return asyncio.run(coro)

    def get_job_info(self, job_id, backend_name=None):
        return {"Id": int(job_id), "Tasks": [], "State": HEAppEJobState.Finished.value}

    async def aget_job_info(self, job_id, backend_name=None):
        return self.get_job_info(job_id, backend_name)

    async def iter_events(self, backend_name=None, heartbeat=30.0):
        """Event websocket that closes without events (subclasses add some)."""
        for event in getattr(self, "events", []):
            yield event

    def get_job_results(self, submitted_job, artifact_names):
        self.requested_artifacts.append(list(artifact_names))
        artifacts = {QJob._COUNTS_ARTIFACT: RAW}
        if QJob._MEAS_ARTIFACT in artifact_names:
            artifacts[QJob._MEAS_ARTIFACT] = MEAS
        return {7: {"artifacts": artifacts}}


class _FakeBackend:
    name = "fake"
    result_to_qiskit = QBackendIQM.result_to_qiskit
    result_to_qiskit_legacy = QBackendIQM.result_to_qiskit_legacy
    _submit = QBackend._submit

    def __init__(self):
        self._qclient = _FakeJobClient()
        self.options = QBackend._default_options()


def _finished_job(memory=None):
    backend = _FakeBackend()
    options = {} if memory is None else {"memory": memory}
    job = backend._submit({"run_request": "{}"}, shots=4, **options)
    job._submitted_job = {"Id": 7, "Tasks": [], "State": HEAppEJobState.Finished.value}
    return job


class TestRunMemory:
    def test_default_options_include_memory(self):
        assert QBackend._default_options().memory is False

    @pytest.mark.parametrize(
        "memory, expected", [(None, False), (False, False), (True, True)]
    )
    def test_submit_passes_memory_to_job(self, memory, expected):
        assert _finished_job(memory)._memory is expected

    def test_memory_true_returns_per_shot_data(self):
        job = _finished_job(memory=True)
        res = job.result()
        assert res.get_memory() == _expected()[1]
        assert job._backend._qclient.requested_artifacts[-1] == [
            QJob._COUNTS_ARTIFACT,
            QJob._MEAS_ARTIFACT,
        ]

    def test_memory_false_skips_measurements(self):
        job = _finished_job()
        job.result()
        assert job._backend._qclient.requested_artifacts == [[QJob._COUNTS_ARTIFACT]]

    def test_result_is_cached(self):
        job = _finished_job(memory=True)
        assert job.result() is job.result()
        assert len(job._backend._qclient.requested_artifacts) == 1

    def test_result_takes_no_arguments(self):
        with pytest.raises(TypeError, match="with_measurements"):
            _finished_job().result(with_measurements=True)

    def test_backend_sampler_v2_gets_per_shot_data(self):
        """Mirrors what BackendSamplerV2 does: run(memory=True), then read Result memory."""
        job = _finished_job(memory=True)
        res = job.result()
        assert res.results[0].data.memory is not None


# ---------- live status ----------


class _StatusClient(_FakeJobClient):
    """Returns the given HEAppE states, one per get_job_info call."""

    def __init__(self, states):
        super().__init__()
        self.states = list(states)
        self.info_calls = 0

    def get_job_info(self, job_id, backend_name=None):
        self.info_calls += 1
        return {"Id": int(job_id), "State": self.states.pop(0)}


def _status_job(states):
    backend = _FakeBackend()
    backend._qclient = _StatusClient(states)
    return backend._submit({"run_request": "{}"}, shots=4)


class TestLiveStatus:
    def test_status_polls_heappe_without_result(self):
        S = HEAppEJobState
        job = _status_job([S.Queued.value, S.Running.value, S.Finished.value])
        assert job.status() == JobStatus.QUEUED
        assert job.status() == JobStatus.RUNNING
        assert job.status() == JobStatus.DONE
        assert job.done() and job.in_final_state()

    def test_no_more_queries_after_terminal_state(self):
        job = _status_job([HEAppEJobState.Failed.value])
        assert job.status() == JobStatus.ERROR
        assert job.status() == JobStatus.ERROR
        assert job._qclient.info_calls == 1

    def test_wait_for_final_state_returns(self):
        S = HEAppEJobState
        job = _status_job([S.Submitted.value, S.Running.value, S.Finished.value])
        job.wait_for_final_state(timeout=5, wait=0)
        assert job.status() == JobStatus.DONE

    def test_missing_state_keeps_last_status(self):
        job = _status_job([])
        job._qclient.get_job_info = lambda job_id, backend_name=None: {"Id": 7}
        assert job.status() == JobStatus.QUEUED


# ---------- 1.1.0: Result by default, QSamplerV2 (issue #49) ----------


class TestResultType:
    def test_result_is_qiskit_result(self):
        from qiskit.result import Result

        res = _finished_job().result()
        assert isinstance(res, Result)
        assert res.get_counts() == _expected()[0]

    def test_qiskit_legacy_argument_removed(self):
        with pytest.raises(TypeError, match="qiskit_legacy"):
            _finished_job().result(qiskit_legacy=False)


def _iqm_measurements(circuit, shots):
    """Fake IQM per-shot data for ``circuit``: one key per clbit, as qiskit-iqm names them.

    A clbit reads 1 if ``circuit.metadata["ones"]`` lists it as ``(creg, bit)``, or, for a
    circuit whose first instruction is an rx gate, if every bit of the circuit is set
    when the angle is above 1. Keys are emitted in reverse order on purpose.
    """
    ones = {tuple(x) for x in (circuit.metadata or {}).get("ones", [])}
    first = circuit.data[0].operation if circuit.data else None
    all_one = first is not None and first.name == "rx" and float(first.params[0]) > 1
    keys = {}
    for idx, creg in enumerate(circuit.cregs):
        for bit in range(creg.size):
            value = int(all_one or (creg.name, bit) in ones)
            keys[f"{creg.name}_{creg.size}_{idx}_{bit}"] = [[value]] * shots
    return dict(reversed(keys.items()))


class _SamplerClient(_FakeJobClient):
    def __init__(self):
        super().__init__()
        self.artifacts = {}

    def get_job_results(self, submitted_job, artifact_names):
        self.requested_artifacts.append(list(artifact_names))
        job_id = submitted_job["Id"]
        return {job_id: {"artifacts": self.artifacts[job_id]}}

    def get_job_info(self, job_id, backend_name=None):
        return {"Id": int(job_id), "State": HEAppEJobState.Finished.value}


class _SamplerBackend:
    """Backend stand-in whose run() returns finished QJobs with fake IQM data."""

    name = "fake"

    def __init__(self, shots=64):
        self._qclient = _SamplerClient()
        self.options = SimpleNamespace(shots=shots, memory=False)
        self.runs = []

    def run(self, circuits, shots, **options):
        job_id = 100 + len(self.runs)
        self.runs.append({"circuits": circuits, "shots": shots, **options})
        self._qclient.artifacts[job_id] = {
            QJob._COUNTS_ARTIFACT: [],
            QJob._MEAS_ARTIFACT: [_iqm_measurements(c, shots) for c in circuits],
        }
        submitted = {"Id": job_id, "Tasks": [], "State": HEAppEJobState.Finished.value}
        return QJob(self, job_id, submitted_job=submitted, shots=shots, memory=True)


def _two_register_circuit(ones):
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

    a, b = ClassicalRegister(2, "a"), ClassicalRegister(1, "b")
    qc = QuantumCircuit(QuantumRegister(3), a, b, metadata={"ones": ones})
    qc.measure([0, 1], a)
    qc.measure(2, b)
    return qc


class TestQSamplerV2:
    def test_registers_and_bit_order(self):
        from qheappe_client import QSamplerV2

        backend = _SamplerBackend()
        qc = _two_register_circuit(ones=[("a", 0), ("b", 0)])
        res = QSamplerV2(backend).run([qc], shots=10).result()
        data = res[0].data
        assert data.a.get_counts() == {"01": 10}  # clbit 0 is rightmost
        assert data.b.get_counts() == {"1": 10}
        assert res[0].metadata["shots"] == 10

    def test_runs_with_memory_and_run_options(self):
        from qheappe_client import QSamplerV2

        backend = _SamplerBackend()
        qc = _two_register_circuit(ones=[])
        QSamplerV2(backend, run_options={"job_name": "s"}).run([qc]).result()
        run = backend.runs[0]
        assert run["memory"] is True and run["job_name"] == "s"
        assert run["shots"] == 64  # backend default
        assert backend._qclient.requested_artifacts == [
            [QJob._COUNTS_ARTIFACT, QJob._MEAS_ARTIFACT]
        ]

    def test_default_shots_override(self):
        from qheappe_client import QSamplerV2

        backend = _SamplerBackend()
        sampler = QSamplerV2(backend, default_shots=7)
        assert (
            sampler.run([_two_register_circuit([])]).result()[0].data.a.num_shots == 7
        )

    def test_parameter_sweep_keeps_pub_shape(self):
        from qiskit import QuantumCircuit
        from qiskit.circuit import Parameter

        from qheappe_client import QSamplerV2

        theta = Parameter("t")
        qc = QuantumCircuit(1)
        qc.rx(theta, 0)
        qc.measure_all()
        backend = _SamplerBackend()
        res = QSamplerV2(backend).run([(qc, [[0.0], [3.0], [0.5]])], shots=5).result()
        meas = res[0].data.meas
        assert meas.shape == (3,)
        assert [meas[i].get_counts() for i in range(3)] == [
            {"0": 5},
            {"1": 5},
            {"0": 5},
        ]
        assert len(backend.runs) == 1 and len(backend.runs[0]["circuits"]) == 3

    def test_one_backend_job_per_shot_count(self):
        from qheappe_client import QSamplerV2

        backend = _SamplerBackend()
        qa = _two_register_circuit(ones=[("a", 1)])
        qb = _two_register_circuit(ones=[("b", 0)])
        job = QSamplerV2(backend).run([(qa, None, 10), (qb, None, 20), (qa, None, 10)])
        assert [r["shots"] for r in backend.runs] == [10, 20]
        assert job.job_id() == "100,101"
        res = job.result()
        assert [r.data.a.num_shots for r in res] == [10, 20, 10]
        assert res[0].data.a.get_counts() == {"10": 10}
        assert res[1].data.b.get_counts() == {"1": 20}
        assert job.result() is res

    def test_status_and_final_state(self):
        from qheappe_client import QSamplerV2

        job = QSamplerV2(_SamplerBackend()).run([_two_register_circuit([])])
        assert job.status() == JobStatus.DONE
        assert job.done() and job.in_final_state()
        assert not job.running() and not job.cancelled()

    def test_status_reports_least_advanced_job(self):
        from qheappe_client.qiskit.qsampler import QSamplerJob

        done = SimpleNamespace(status=lambda: JobStatus.DONE, job_id=lambda: "1")
        queued = SimpleNamespace(status=lambda: JobStatus.QUEUED, job_id=lambda: "2")
        job = QSamplerJob([], [(done, []), (queued, [])])
        assert job.status() == JobStatus.QUEUED and not job.in_final_state()

    def test_circuit_without_registers_rejected(self):
        from qiskit import QuantumCircuit

        from qheappe_client import QSamplerV2

        with pytest.raises(ValueError, match="no classical registers"):
            QSamplerV2(_SamplerBackend()).run([QuantumCircuit(1)])

    def test_unknown_measurement_key_rejected(self):
        from qheappe_client import QSamplerV2

        backend = _SamplerBackend()
        job = QSamplerV2(backend).run([_two_register_circuit([])], shots=2)
        backend._qclient.artifacts[100][QJob._MEAS_ARTIFACT] = [
            {"zz_1_5_0": [[0], [1]]}
        ]
        with pytest.raises(ValueError, match="does not match"):
            job.result()


# Live data for x(0) measured into a(2) + b(1): the server sends bits in key order,
# so clbit 0 comes first ("100"); Qiskit expects clbit 0 rightmost ("001").
IQM_RAW = [
    {"measurement_keys": ["a_2_0_0", "a_2_0_1", "b_1_1_0"], "counts": {"100": 3}}
]
IQM_MEAS = [{"a_2_0_0": [[1]] * 3, "a_2_0_1": [[0]] * 3, "b_1_1_0": [[0]] * 3}]


class TestIQMKeyBitOrder:
    def test_legacy_counts_follow_qiskit_order(self):
        res = QBackendIQM.result_to_qiskit_legacy(None, IQM_RAW)
        assert res.get_counts() == {"0 01": 3}

    def test_legacy_memory_follows_qiskit_order(self):
        res = QBackendIQM.result_to_qiskit_legacy(
            None, IQM_RAW, measurements_raw=IQM_MEAS
        )
        assert res.get_memory() == ["0 01"] * 3
        assert (
            res.results[0].header["measurement_keys"] == IQM_RAW[0]["measurement_keys"]
        )

    def test_key_order_does_not_matter(self):
        raw = [
            {
                "measurement_keys": ["b_1_1_0", "a_2_0_1", "a_2_0_0"],
                "counts": {"001": 3},
            }
        ]
        res = QBackendIQM.result_to_qiskit_legacy(None, raw)
        assert res.get_counts() == {"0 01": 3}

    def test_primitive_bit_order(self):
        for meas in (None, IQM_MEAS):
            res = QBackendIQM.result_to_qiskit(None, IQM_RAW, measurements_raw=meas)
            assert res[0].data.meas.get_counts() == {"001": 3}
            assert res[0].metadata["measurement_keys"] == IQM_RAW[0]["measurement_keys"]

    def test_unused_register_bits_are_zero(self):
        raw = [{"measurement_keys": ["c_3_0_2"], "counts": {"1": 4}}]
        res = QBackendIQM.result_to_qiskit_legacy(None, raw)
        assert res.get_counts() == {"100": 4}


def test_result_has_no_key_order_argument():
    with pytest.raises(TypeError, match="key_order_left_to_right"):
        _finished_job().result(key_order_left_to_right=True)


# ---------- Qiskit interface: cancel, default shots ----------


class _CancelClient(_FakeJobClient):
    def __init__(self, accept):
        super().__init__()
        self.accept = accept
        self.cancelled = []

    async def cancel_job(self, job_id, backend_name=None):
        self.cancelled.append(job_id)
        return self.accept

    def get_job_info(self, job_id, backend_name=None):
        return {"Id": int(job_id), "State": HEAppEJobState.Running.value}


def _running_job(accept):
    backend = _FakeBackend()
    backend._qclient = _CancelClient(accept)
    return backend._submit({"run_request": "{}"}, shots=4)


class TestCancel:
    def test_cancel_reaches_client_and_reports_cancelled(self):
        job = _running_job(accept=True)
        assert job.cancel() is True
        assert job._qclient.cancelled == [7]
        assert job.status() == JobStatus.CANCELLED
        assert job.cancelled() and job.in_final_state()

    def test_refused_cancel_keeps_server_status(self):
        job = _running_job(accept=False)
        assert job.cancel() is False
        assert job.status() == JobStatus.RUNNING

    def test_failed_after_cancel_maps_to_cancelled(self):
        job = _running_job(accept=True)
        job.cancel()
        assert job._to_qiskit_status("FAILED") == JobStatus.CANCELLED


class TestRunDefaultShots:
    def _backend(self):
        from qiskit.providers.options import Options

        calls = {}
        fake = SimpleNamespace(
            options=Options(shots=321, memory=False),
            create_circuit_job_definition=lambda **kw: (
                calls.setdefault("def", kw)
                and SimpleNamespace(model_dump_json=lambda: "{}")
            ),
            _submit=lambda payloads, shots, **opts: calls.setdefault("shots", shots),
        )
        return fake, calls

    def test_shots_default_to_backend_option(self):
        from qiskit import QuantumCircuit

        fake, calls = self._backend()
        QBackendIQM.run(fake, QuantumCircuit(1))
        assert calls["shots"] == 321 and calls["def"]["shots"] == 321

    def test_explicit_shots_win(self):
        from qiskit import QuantumCircuit

        fake, calls = self._backend()
        QBackendIQM.run(fake, QuantumCircuit(1), shots=5)
        assert calls["shots"] == 5


# ---------- Result header: registers, circuit name and metadata ----------


def _one_hot():
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

    a, b = ClassicalRegister(2, "a"), ClassicalRegister(1, "b")
    qc = QuantumCircuit(QuantumRegister(3), a, b, name="onehot", metadata={"tag": 1})
    qc.x(0)
    qc.measure([0, 1], a)
    qc.measure(2, b)
    return qc


class TestResultHeader:
    def test_matches_qiskit_basic_simulator(self):
        from qiskit.providers.basic_provider import BasicSimulator

        qc = _one_hot()
        ref = BasicSimulator().run(qc, shots=3, memory=True).result()
        ours = QBackendIQM.result_to_qiskit_legacy(
            None,
            IQM_RAW,
            measurements_raw=IQM_MEAS,
            circuit_headers=[{"name": qc.name, "metadata": qc.metadata}],
        )
        assert ours.get_counts() == ref.get_counts() == {"0 01": 3}
        assert ours.get_memory() == ref.get_memory()
        for field in ("name", "creg_sizes", "memory_slots", "metadata"):
            assert ours.results[0].header[field] == ref.results[0].header[field], field
        assert ours.get_counts(qc) == ref.get_counts(qc)

    def test_without_circuit_headers(self):
        res = QBackendIQM.result_to_qiskit_legacy(None, IQM_RAW)
        header = res.results[0].header
        assert header["name"] == "circuit-0" and header["metadata"] == {}
        assert header["creg_sizes"] == [["a", 2], ["b", 1]]

    def test_mismatched_circuit_headers_are_ignored(self):
        res = QBackendIQM.result_to_qiskit_legacy(
            None, IQM_RAW, circuit_headers=[{"name": "x"}, {"name": "y"}]
        )
        assert res.results[0].header["name"] == "circuit-0"

    def test_non_iqm_keys_use_one_register(self):
        res = QBackendIQM.result_to_qiskit_legacy(None, RAW)
        assert res.results[0].header["creg_sizes"] == [["meas", 2]]

    def test_run_passes_circuit_headers_to_job(self):
        from qiskit.providers.options import Options

        calls = {}
        fake = SimpleNamespace(
            options=Options(shots=10, memory=False),
            create_circuit_job_definition=lambda **kw: SimpleNamespace(
                model_dump_json=lambda: "{}"
            ),
            _submit=lambda payloads, shots, **opts: calls.update(opts),
        )
        qc = _one_hot()
        QBackendIQM.run(fake, [qc])
        assert calls["circuit_headers"] == [{"name": "onehot", "metadata": {"tag": 1}}]

    def test_job_result_uses_circuit_headers(self):
        job = _finished_job()
        job._circuit_headers = [{"name": "bell", "metadata": {"k": "v"}}]
        header = job.result().results[0].header
        assert header["name"] == "bell" and header["metadata"] == {"k": "v"}


# ---------- waiting for the terminal state (stale submit-time State) ----------


class _WaitClient(_FakeJobClient):
    """HEAppE stand-in whose job info returns ``states`` in turn (last one repeats)."""

    def __init__(self, states):
        super().__init__()
        self.states = list(states)
        self.info_calls = 0
        self.waits = 0

    def get_job_info(self, job_id, backend_name=None):
        self.info_calls += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"Id": int(job_id), "Tasks": [{"Id": 1}], "State": state.value}

    async def iter_events(self, backend_name=None, heartbeat=30.0):
        self.waits += 1  # one websocket opened per wait
        yield {"data": {"jobId": "999", "state": "Running"}}  # another job
        yield {"data": {"jobId": "7", "state": "Finished"}}  # mixed case, as HEAppE


def _job_with(states):
    backend = _FakeBackend()
    backend._qclient = _WaitClient(states)
    job = backend._submit({"run_request": "{}"}, shots=4)
    # submit-time info: State is stale (Configuring) although the job has moved on
    job._submitted_job = {
        "Id": 7,
        "Tasks": [],
        "State": HEAppEJobState.Configuring.value,
    }
    return job


class TestWaitForTerminalState:
    def test_finished_job_is_not_waited_on(self):
        job = _job_with([HEAppEJobState.Finished])
        job.result()
        assert job._qclient.waits == 0
        assert job._submitted_job["State"] == HEAppEJobState.Finished.value

    def test_running_job_waits_then_refreshes_info(self):
        S = HEAppEJobState
        job = _job_with([S.Running, S.Finished])
        job.result()
        assert job._qclient.waits == 1
        assert job._submitted_job["State"] == S.Finished.value
        assert job._submitted_job["Tasks"] == [{"Id": 1}]

    def test_state_seen_by_status_is_reused(self):
        job = _job_with([HEAppEJobState.Finished])
        assert job.status() == JobStatus.DONE
        calls = job._qclient.info_calls
        job.result()
        assert job._qclient.info_calls == calls and job._qclient.waits == 0
        assert job._submitted_job["State"] == HEAppEJobState.Finished.value

    def test_streamed_state_used_when_info_has_none(self):
        job = _job_with([HEAppEJobState.Running])
        job._qclient.get_job_info = lambda job_id, backend_name=None: (
            {"Id": 7} if job._qclient.waits else {"Id": 7, "State": 2}
        )
        job.result()
        assert job._last_known_status == "FINISHED"

    def test_failed_job_raises(self):
        job = _job_with([HEAppEJobState.Failed])
        with pytest.raises(RuntimeError, match="FAILED"):
            job.result()


def test_list_available_backends_connects_and_closes(monkeypatch):
    from qheappe_client import qprovider

    events = []

    class _Client:
        def __init__(self, *args, **kwargs):
            events.append("init")

        def __enter__(self):
            events.append("connect")
            return self

        def __exit__(self, *exc):
            events.append("close")

        def get_available_backends(self):
            events.append("list")
            return {"VLQ": "metadata"}

    monkeypatch.setattr(qprovider, "QClient", _Client)
    result = qprovider.QProvider.list_available_backends("t", "p", lexis_resource="r")
    assert result == {"VLQ": "metadata"}
    assert events == ["init", "connect", "list", "close"]


# ---------- wait_for_final_state over the event websocket ----------


class _EventClient(_FakeJobClient):
    """Job info stays SUBMITTED; only the websocket reports progress."""

    def __init__(
        self, events=(), fail_stream=False, info_state=HEAppEJobState.Submitted
    ):
        super().__init__()
        self.events = list(events)
        self.fail_stream = fail_stream
        self.info_state = info_state
        self.streams = 0

    def get_job_info(self, job_id, backend_name=None):
        return {"Id": int(job_id), "Tasks": [], "State": self.info_state.value}

    async def iter_events(self, backend_name=None, heartbeat=30.0):
        self.streams += 1
        if self.fail_stream:
            raise QException("websocket refused")
        for event in self.events:
            await asyncio.sleep(0)
            yield event
        await asyncio.Event().wait()  # stays open, like a live websocket


def _event_job(**kwargs):
    backend = _FakeBackend()
    backend._qclient = _EventClient(**kwargs)
    return backend._submit({"run_request": "{}"}, shots=4)


def _ev(state, job_id="7"):
    return {"data": {"jobId": job_id, "state": state}}


class TestWaitForFinalStateEvents:
    def test_follows_events_and_reports_changes(self):
        job = _event_job(
            events=[_ev("Queued"), _ev("Running"), _ev("Running"), _ev("Finished")]
        )
        seen = []
        job.wait_for_final_state(wait=60, callback=lambda *a: seen.append(a[1]))
        assert seen == [JobStatus.QUEUED, JobStatus.RUNNING]  # start, then each change
        assert job.status() == JobStatus.DONE
        assert job._qclient.streams == 1

    def test_ignores_other_jobs(self):
        job = _event_job(events=[_ev("Finished", job_id="8"), _ev("Failed")])
        job.wait_for_final_state(wait=60)
        assert job.status() == JobStatus.ERROR

    def test_finished_job_opens_no_stream(self):
        job = _event_job(info_state=HEAppEJobState.Finished)
        job.wait_for_final_state()
        assert job._qclient.streams == 0

    def test_job_info_catches_missed_events(self):
        job = _event_job()  # websocket silent, e.g. job finished before it connected
        job._qclient.info_state = HEAppEJobState.Submitted
        calls = []

        def info(job_id, backend_name=None):
            calls.append(1)
            state = (
                HEAppEJobState.Submitted if len(calls) == 1 else HEAppEJobState.Finished
            )
            return {"Id": 7, "Tasks": [], "State": state.value}

        job._qclient.get_job_info = info
        job.wait_for_final_state(wait=0.01, timeout=5)
        assert job.status() == JobStatus.DONE

    def test_stream_failure_falls_back_to_job_info(self):
        job = _event_job(fail_stream=True)
        states = iter([HEAppEJobState.Submitted, HEAppEJobState.Finished])
        last = [HEAppEJobState.Submitted]

        def info(job_id, backend_name=None):
            last[0] = next(states, last[0])
            return {"Id": 7, "Tasks": [], "State": last[0].value}

        job._qclient.get_job_info = info
        job.wait_for_final_state(wait=0.01, timeout=5)
        assert job.status() == JobStatus.DONE

    def test_timeout_raises_job_timeout_error(self):
        from qiskit.providers.exceptions import JobTimeoutError

        job = _event_job()
        with pytest.raises(JobTimeoutError):
            job.wait_for_final_state(timeout=0.05, wait=60)

    def test_result_waits_on_events(self):
        job = _event_job(events=[_ev("Finished")])
        assert sum(job.result().get_counts().values()) == 3
        assert job._qclient.streams == 1


class TestSamplerJobWait:
    def test_waits_for_all_jobs_and_reports_combined_status(self):
        from qheappe_client.qiskit.qsampler import QSamplerJob

        jobs = [_event_job(events=[_ev("Running"), _ev("Finished")]) for _ in range(2)]
        sampler_job = QSamplerJob([], [(j, []) for j in jobs])
        seen = []
        sampler_job.wait_for_final_state(wait=60, callback=lambda *a: seen.append(a))
        assert sampler_job.done()
        assert all(a[0] == sampler_job.job_id() and a[2] is sampler_job for a in seen)
        assert seen

    def test_timeout_is_shared(self):
        from qiskit.providers.exceptions import JobTimeoutError

        from qheappe_client.qiskit.qsampler import QSamplerJob

        sampler_job = QSamplerJob([], [(_event_job(), [])])
        with pytest.raises(JobTimeoutError):
            sampler_job.wait_for_final_state(timeout=0.05, wait=60)
