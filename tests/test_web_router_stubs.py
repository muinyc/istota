"""The shared router auth/CSRF stubs, and the two identity rules that go with them.

Five module routers declared ``require_auth`` and ``verify_origin`` with
byte-identical bodies. Folding them changes one thing that is not visible in a
diff: ``dependency_overrides`` is keyed by the callable, so five declarations
were five keys and one declaration is one. That collapse is safe only because
``web_app.py`` sets every one of them to the same two values — which is
asserted here rather than assumed, since the day one router needs a different
gate is the day this stops being true.

The mirror-image rule is that ``make_get_user_context`` must return a
**distinct** object per module, because the route tests override one module's
context per app.
"""

import ast
import pathlib

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from istota import garmin_routes, web_router_stubs
from istota.briefings import routes as briefings_routes
from istota.feeds import routes as feeds_routes
from istota.health import routes as health_routes
from istota.money import routes as money_routes

ROUTERS = {
    "briefings": briefings_routes,
    "feeds": feeds_routes,
    "garmin": garmin_routes,
    "health": health_routes,
    "money": money_routes,
}

WITH_CONTEXT = {
    "briefings": briefings_routes,
    "feeds": feeds_routes,
    "health": health_routes,
}


class _FakeSession(dict):
    pass


class _FakeRequest:
    def __init__(self, session=None, state=None):
        if session is not None:
            self.session = session
        self.app = type("App", (), {"state": state or type("S", (), {})()})()


class TestTheStubItself:
    def test_no_session_middleware_is_a_401_not_an_attribute_error(self):
        with pytest.raises(HTTPException) as exc:
            web_router_stubs.require_auth(_FakeRequest())
        assert exc.value.status_code == 401

    def test_an_empty_session_is_a_401(self):
        with pytest.raises(HTTPException) as exc:
            web_router_stubs.require_auth(_FakeRequest(session=_FakeSession()))
        assert exc.value.status_code == 401

    def test_a_session_user_comes_back_whole(self):
        user = {"username": "alice", "display_name": "Alice"}
        req = _FakeRequest(session=_FakeSession(user=user))
        assert web_router_stubs.require_auth(req) is user

    def test_verify_origin_is_a_no_op_seam(self):
        assert web_router_stubs.verify_origin(_FakeRequest()) is None


class TestTheFiveRoutersShareOneObject:
    """The intended collapse, asserted so it is a decision rather than a
    coincidence of five modules importing the same name."""

    @pytest.mark.parametrize("name", sorted(ROUTERS))
    def test_require_auth(self, name):
        assert ROUTERS[name].require_auth is web_router_stubs.require_auth

    @pytest.mark.parametrize("name", sorted(ROUTERS))
    def test_verify_origin(self, name):
        assert ROUTERS[name].verify_origin is web_router_stubs.verify_origin

    def test_web_app_overrides_them_to_one_answer_each(self):
        """The collapse is only inert while every router gets the same value.
        Five keys mapping to two values is now two keys mapping to two values;
        a router wanting its own gate declares one of its own, as
        ``briefings.require_admin`` does."""
        from istota import web_app

        overrides = web_app.app.dependency_overrides
        assert overrides[web_router_stubs.require_auth] is web_app._require_api_auth
        assert overrides[web_router_stubs.verify_origin] is web_app._verify_origin

    def test_briefings_keeps_its_own_admin_gate(self):
        """The counter-example: a router whose extra gate is not shared."""
        assert briefings_routes.require_admin is not web_router_stubs.require_auth


class TestEachContextDependencyStaysItsOwnObject:
    """A factory returning one cached closure would let a test overriding
    health's context reach feeds' routes on the same app."""

    def test_the_three_are_pairwise_distinct(self):
        deps = [m.get_user_context for m in WITH_CONTEXT.values()]
        assert len({id(d) for d in deps}) == 3

    def test_two_factory_calls_are_not_the_same_object(self):
        made = [
            web_router_stubs.make_get_user_context(
                cache_attr="x_initialised_dbs",
                resolve=lambda uid, cfg: None,
                ensure=lambda ctx, cfg: None,
                not_found=KeyError,
            )
            for _ in range(2)
        ]
        assert made[0] is not made[1]


class _Ctx:
    def __init__(self, db_path):
        self.db_path = db_path
        self.dirs_ensured = 0

    def ensure_dirs(self):
        self.dirs_ensured += 1


class _NotFound(Exception):
    pass


def _app_with(dep, cache_attr):
    app = FastAPI()

    @app.get("/probe")
    def probe(ctx=Depends(dep)):
        return {"db_path": str(ctx.db_path), "dirs_ensured": ctx.dirs_ensured}

    app.dependency_overrides[web_router_stubs.require_auth] = lambda: {"username": "alice"}
    app.state.istota_config = object()
    return app


class TestTheFactoryKeepsTheBehaviourItReplaced:
    def test_initialisation_runs_once_per_db_path_then_ensure_dirs(self):
        ctx = _Ctx("/tmp/alice.db")
        ensured: list = []
        dep = web_router_stubs.make_get_user_context(
            cache_attr="probe_initialised_dbs",
            resolve=lambda uid, cfg: ctx,
            ensure=lambda c, cfg: ensured.append((c, cfg)),
            not_found=_NotFound,
        )
        app = _app_with(dep, "probe_initialised_dbs")
        with TestClient(app) as client:
            first = client.get("/probe")
            second = client.get("/probe")
        assert first.status_code == 200
        assert len(ensured) == 1, "ensure_initialised must not run per request"
        assert ensured[0][1] is app.state.istota_config
        assert second.json()["dirs_ensured"] == 1
        assert app.state.probe_initialised_dbs == {"/tmp/alice.db"}

    def test_a_second_db_path_initialises_again(self):
        seen = []
        contexts = iter([_Ctx("/tmp/a.db"), _Ctx("/tmp/b.db")])
        dep = web_router_stubs.make_get_user_context(
            cache_attr="probe_initialised_dbs",
            resolve=lambda uid, cfg: next(contexts),
            ensure=lambda c, cfg: seen.append(c.db_path),
            not_found=_NotFound,
        )
        with TestClient(_app_with(dep, "probe_initialised_dbs")) as client:
            client.get("/probe")
            client.get("/probe")
        assert seen == ["/tmp/a.db", "/tmp/b.db"]

    def test_the_modules_own_not_found_becomes_a_404_carrying_its_text(self):
        def resolve(uid, cfg):
            raise _NotFound("health is off for alice")

        dep = web_router_stubs.make_get_user_context(
            cache_attr="probe_initialised_dbs",
            resolve=resolve,
            ensure=lambda c, cfg: None,
            not_found=_NotFound,
        )
        with TestClient(_app_with(dep, "probe_initialised_dbs")) as client:
            r = client.get("/probe")
        assert r.status_code == 404
        assert r.json()["detail"] == "health is off for alice"

    def test_another_modules_error_is_not_swallowed(self):
        """Each loader keeps its own ``UserNotFoundError`` class precisely so
        one module's refusal does not read as another's."""

        class OtherModuleError(Exception):
            pass

        def resolve(uid, cfg):
            raise OtherModuleError("not mine")

        dep = web_router_stubs.make_get_user_context(
            cache_attr="probe_initialised_dbs",
            resolve=resolve,
            ensure=lambda c, cfg: None,
            not_found=_NotFound,
        )
        with TestClient(
            _app_with(dep, "probe_initialised_dbs"), raise_server_exceptions=False
        ) as client:
            r = client.get("/probe")
        assert r.status_code == 500

    def test_the_cache_attribute_is_per_module(self):
        """Two modules in one process must not shadow each other's set."""
        app = FastAPI()
        for name in ("alpha", "beta"):
            ctx = _Ctx(f"/tmp/{name}.db")
            dep = web_router_stubs.make_get_user_context(
                cache_attr=f"{name}_initialised_dbs",
                resolve=lambda uid, cfg, c=ctx: c,
                ensure=lambda c, cfg: None,
                not_found=_NotFound,
            )

            def endpoint(ctx=Depends(dep)):
                return {"db_path": str(ctx.db_path)}

            app.add_api_route(f"/{name}", endpoint)
        app.dependency_overrides[web_router_stubs.require_auth] = lambda: {"username": "a"}
        app.state.istota_config = object()
        with TestClient(app) as client:
            assert client.get("/alpha").json() == {"db_path": "/tmp/alpha.db"}
            assert client.get("/beta").json() == {"db_path": "/tmp/beta.db"}
        assert app.state.alpha_initialised_dbs == {"/tmp/alpha.db"}
        assert app.state.beta_initialised_dbs == {"/tmp/beta.db"}


class TestNoSixthCopy:
    """The stub body, anywhere under ``src/istota/`` other than the shared
    module, is a router that has declared its own again. Matched on the shape
    of the body rather than on the function name, so a rename does not hide."""

    def test_only_web_router_stubs_raises_the_401(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "web_router_stubs.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                handlers = [
                    tuple(sorted(
                        n.id for n in ast.walk(h.type or ast.Tuple(elts=[]))
                        if isinstance(n, ast.Name)
                    ))
                    for h in node.handlers
                ]
                if ("AssertionError", "AttributeError") in handlers and any(
                    'session' in ast.dump(s) for s in node.body
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert offenders == [], (
            "a router has declared its own session-reading auth stub again: "
            f"{offenders}"
        )

    def test_no_other_module_defines_either_stub_by_name(self):
        """The body-shape guard above catches a copy under a new name; this
        catches the likelier one, which keeps the name — ``web_app.py`` imports
        both by name to key `dependency_overrides` on them, so a router copying
        a stub back copies the name with it. ``verify_origin`` has no
        distinctive body at all (``return None``), so the shape guard cannot
        see it and forking the CSRF key would otherwise be silent."""
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "web_router_stubs.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name in ("require_auth", "verify_origin")
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")
        assert offenders == [], (
            "a router has declared its own copy of a shared stub, which forks "
            f"the dependency_overrides key it is registered under: {offenders}"
        )

    def test_that_guard_is_looking_at_the_right_names(self):
        """Positive control: both names must exist in the shared module, or
        the walk above is asserting the absence of something that never was."""
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src" / "istota" / "web_router_stubs.py"
        ).read_text()
        defined = {
            n.name for n in ast.walk(ast.parse(source))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert {"require_auth", "verify_origin"} <= defined

    def test_the_guard_matches_the_real_stub(self):
        """Without this, renaming the exception tuple would leave the guard
        green against every copy it is supposed to catch."""
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src" / "istota" / "web_router_stubs.py"
        ).read_text()
        tree = ast.parse(source)
        hits = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Try)
            and any(
                tuple(sorted(
                    x.id for x in ast.walk(h.type or ast.Tuple(elts=[]))
                    if isinstance(x, ast.Name)
                )) == ("AssertionError", "AttributeError")
                for h in n.handlers
            )
            and any("session" in ast.dump(s) for s in n.body)
        ]
        assert len(hits) == 1
