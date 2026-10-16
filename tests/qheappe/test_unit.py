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
from qheappe_client.qiskit.qbackend import HEAppE_QISKIT_STATUS_MAPPING, _HEAppEJobId
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
        assert out == [("b", "true"), ("f", "false"), ("u", str(u)), ("i", 3), ("s", "x")]

    def test_list_repeats_key_and_skips_none(self):
        assert normalize_query({"k": ["x", None, "y"], "t": (1, 2)}) == [
            ("k", "x"), ("k", "y"), ("t", 1), ("t", 2)
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
    assert req_describe_body(None, aiohttp.MultipartWriter("form-data")) == " body=multipart/form-data"
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
        data = [full("ClusterExt", Id=1, Name="VLQ",
                     NodeTypes=[full("ClusterNodeTypeExt", Id=5, Name="qpu")])]
        (cluster,) = from_swagger_json(data, "list[ClusterExt]")
        assert isinstance(cluster, heappe_models.ClusterExt)
        assert (cluster.id, cluster.name) == (1, "VLQ")
        assert cluster.node_types[0].id == 5

    @pytest.mark.xfail(strict=True, raises=KeyError,
                       reason="strict data[json_key]: a field missing in the response "
                              "(py4heappe/HEAppE version skew) raises KeyError")
    def test_missing_field_tolerated(self):
        data = full("ClusterExt", Id=1, Name="VLQ", NodeTypes=[])
        data.pop("Description")
        assert from_swagger_json(data, "ClusterExt").id == 1


# ---------- utils: states / exceptions ----------


class TestHEAppEJobState:
    @pytest.mark.parametrize(
        ("value", "name"),
        [(1, "CONFIGURING"), (2, "SUBMITTED"), (4, "QUEUED"), (8, "RUNNING"),
         (16, "FINISHED"), (32, "FAILED"), (64, "CANCELED")],
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
    assert HttpResult(200, "application/octet-stream", b"\x00raw").parsed() == b"\x00raw"


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
        token = jwt.encode({"iss": "https://issuer.example"}, "secret", algorithm="HS256")
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
        user, info = run(authed_client([{"ShortName": "proj"}])._authenticate_authorize_lexis())
        assert user == "user" and info["ShortName"] == "proj"

    def test_filter_is_not_trusted(self):
        c = authed_client([{"ShortName": "other"}, {"ShortName": "x"}])
        with pytest.raises(QAuthException, match=r"does not have access.*\['other', 'x'\]"):
            run(c._authenticate_authorize_lexis())

    def test_active_project(self):
        c = authed_client([{"ShortName": "proj", "StartDate": "2000-01-01T00:00:00",
                            "EndDate": "2999-01-01T00:00:00"}])
        assert run(c._authenticate_authorize_lexis())[0] == "user"

    def test_expired_project(self):
        c = authed_client([{"ShortName": "proj", "StartDate": "2000-01-01",
                            "EndDate": "2001-01-01"}])
        with pytest.raises(QAuthException, match="expired"):
            run(c._authenticate_authorize_lexis())

    def test_not_started_project(self):
        c = authed_client([{"ShortName": "proj", "StartDate": "2998-01-01",
                            "EndDate": "2999-01-01"}])
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
    a = {"LocationTypeId": loc, "LocationName": name, "ProjectResourceId": "pr-1",
         "AllocationAmount": 10, "Specifications": [{"Key": "HEAPPE_URL", "Value": url}]}
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
        c = resource_client([
            resource("HPC", assignment("Karolina", loc=1)),
            resource("Q", assignment("VLQ"), assignment("MOCK", url="https://h2"),
                     assignment("IGNORED", loc=1)),
        ])
        r = run(c._authorize_lexis_resource(None))
        assert r.resource_name == "Q"
        assert r.cluster_names == ["MOCK", "VLQ"]
        assert r.clusters["MOCK"].heappe_url == "https://h2"
        assert r.allocated_units == 20 and r.project_resource_id == "pr-1"

    def test_named_resource(self):
        c = resource_client([resource("Q1", assignment("A")), resource("Q2", assignment("B"))])
        assert run(c._authorize_lexis_resource("Q2")).cluster_names == ["B"]

    def test_ambiguous_without_name(self):
        c = resource_client([resource("Q1", assignment("A")), resource("Q2", assignment("B"))])
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
        c = resource_client([resource("Q", bad, assignment("VLQ", url="https://first"),
                                      assignment("VLQ", url="https://second"))])
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
        c = resource_client([resource("Q", assignment("A"), StartDate="2000-01-01",
                                      EndDate="2001-01-01")])
        with pytest.raises(QAuthException, match="active dates"):
            run(c._authorize_lexis_resource(None))

    def test_unparsable_resource_dates_are_lenient(self):
        c = resource_client([resource("Q", assignment("A"), StartDate="x", EndDate="y")])
        assert run(c._authorize_lexis_resource(None)).resource_name == "Q"

    def test_requires_authentication(self):
        with pytest.raises(QAuthException):
            run(QClient("tok", "proj")._authorize_lexis_resource(None))

    @pytest.mark.xfail(strict=True, raises=TypeError,
                       reason="QAuthException.from_cause() is called with reason=..., "
                              "which it does not accept")
    def test_request_failure_maps_to_auth_exception(self):
        c = QClient("tok", "proj")
        c._authenticated = True
        fake_routes(c, {"/userorg/api/ProjectResource": QException("HTTP 503")})
        with pytest.raises(QAuthException):
            run(c._authorize_lexis_resource(None))


# ---------- HEAppE discovery, project id, machine info ----------


def connected_client(*clusters: QCluster, resource_name="Q") -> QClient:
    c = QClient("tok", "proj")
    c._authenticated = True
    c._lexis_resource = LexisResource(resource_name, "pr", "-", "-",
                                      {cl.name: cl for cl in clusters})
    return c


def cluster(name="VLQ", url="https://h1", **kw) -> QCluster:
    return QCluster(name=name, heappe_url=url, **kw)


def heappe_cluster(name, cid, node_type_id):
    return full("ClusterExt", Id=cid, Name=name,
                NodeTypes=[full("ClusterNodeTypeExt", Id=node_type_id, Name="qpu")])


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
        calls = fake_routes(c, {"/ListAvailableClusters": http(
            [heappe_cluster("A", 1, 10), heappe_cluster("B", 2, 20), heappe_cluster("C", 3, 30)]
        )})
        run(c._resolve_heappe_clusters())
        assert len(calls) == 2
        assert [x.cluster_node_type_id for x in (a, b, other)] == [10, 20, 30]

    def test_cluster_not_exposed(self):
        vlq = cluster()
        c = connected_client(vlq)
        fake_routes(c, {"/ListAvailableClusters": http([heappe_cluster("OTHER", 1, 1)])})
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
        vlq = self._fetch(http({"type": "iqm", "version": {"qscheduler_version": "1.0.0"}}))
        assert vlq.available and vlq.error is None
        assert vlq.machine_info.supplier == "IQM"

    def test_unsupported_stack(self):
        vlq = self._fetch(http({"type": "other", "version": {"qscheduler_version": "1.0.0"}}))
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
    vlq = cluster(url=url, cluster_id=1, cluster_node_type_id=2, heappe_project_id=3,
                  machine_info=QMachineInfo.from_response(
                      {"type": "iqm", "version": {"qscheduler_version": "1.0.0"}}),
                  available=True)

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
            assert c.get_job_info(3) == {"Id": 3}  # client loop -> forwarded to app loop
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
            "IQM", "SUPERCONDUCTING", "1.2.3"
        )

    def test_machine_info_unknown_stack(self):
        mi = QMachineInfo.from_response({"type": "other"})
        assert mi.supplier == "UNKNOWN" and mi.qscheduler_version is None

    def test_resource_clusters(self):
        r = LexisResource("R", "id", "-", "-", {
            "B": cluster(name="B", allocated_units=5),
            "A": cluster(name="A"),
        })
        assert r.cluster_names == ["A", "B"]
        assert r.allocated_units == 5
        with pytest.raises(KeyError, match="Available"):
            r.get_cluster("X")

    def test_backend_metadata_without_machine_info(self):
        md = QBackendMetadata(LexisProject("P"), LexisResource("R", "id", "-", "-"), cluster())
        assert md.backend_name == "VLQ"
        assert md.software_stack is None and md.supplier == "-" and not md.available

    def test_split_target(self):
        md = QBackendMetadata(LexisProject("P"), LexisResource("R", "id", "-", "-"), cluster())
        assert _split_target("R", "VLQ") == ("R", "VLQ")
        assert _split_target(md, None) == ("R", "VLQ")
        assert _split_target(md, "VLQ") == ("R", "VLQ")
        with pytest.raises(ValueError, match="conflicts"):
            _split_target(md, "OTHER")


# ---------- job ids / status mapping ----------


def test_heappe_job_id_is_int_and_callable():
    jid = _HEAppEJobId(66)
    assert jid == 66 and jid() == "66"


@pytest.mark.xfail(strict=True, reason="QUEUED / WAITINGFORSERVICEACCOUNT missing in status mapping")
def test_every_heappe_state_is_mapped():
    missing = {s.name.upper() for s in HEAppEJobState} - set(HEAppE_QISKIT_STATUS_MAPPING)
    assert not missing, missing


@pytest.mark.xfail(strict=True, reason="QJob.status() uses JobStatus.CANCELED; Qiskit only has CANCELLED")
def test_qiskit_has_canceled_alias():
    assert hasattr(JobStatus, "CANCELED")


# ---------- result conversion ----------
# Raw artifact formats as consumed by QJob.result():
#   measurement_counts: [{"measurement_keys": [...], "counts": {"01": n, ...}}]
#   measurements:       [{key: [[bit, ...], ...shots]}]

RAW = [{"measurement_keys": ["m"], "counts": {"01": 2, "10": 1}}]
MEAS = [{"m": [[0, 1], [0, 1], [1, 0]]}]


def _expected(left_to_right: bool):
    flip = (lambda s: s[::-1]) if left_to_right else (lambda s: s)
    counts = {flip(k): v for k, v in RAW[0]["counts"].items()}
    memory = [flip(s) for s in ("01", "01", "10")]
    return counts, memory


@pytest.mark.parametrize("left_to_right", [False, True])
class TestResultConversion:
    def test_primitive_counts_path(self, left_to_right):
        res = QBackendIQM.result_to_qiskit(None, RAW, key_order_left_to_right=left_to_right)
        counts, _ = _expected(left_to_right)
        assert res[0].data.meas.get_counts() == counts
        assert res[0].metadata["measurement_keys"] == ["m"]

    def test_primitive_measurement_path_agrees(self, left_to_right):
        res = QBackendIQM.result_to_qiskit(
            None, RAW, measurements_raw=MEAS, key_order_left_to_right=left_to_right
        )
        counts, memory = _expected(left_to_right)
        assert res[0].data.meas.get_counts() == counts
        assert res[0].data.meas.get_bitstrings() == memory

    def test_legacy(self, left_to_right):
        res = QBackendIQM.result_to_qiskit_legacy(
            None, RAW, measurements_raw=MEAS, key_order_left_to_right=left_to_right,
            backend_name="b", job_id="1",
        )
        counts, memory = _expected(left_to_right)
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
            QBackendIQM.result_to_qiskit(None, raw, measurements_raw=[{"a": [[0]], "b": [[0], [1]]}])