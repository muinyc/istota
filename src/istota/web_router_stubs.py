"""The auth and CSRF stubs every module router declares so it stays mountable alone.

``briefings/routes.py``, ``feeds/routes.py``, ``garmin_routes.py``,
``health/routes.py`` and ``money/routes.py`` each declared its own
``require_auth`` and ``verify_origin`` with byte-identical bodies — ten
functions, one behaviour. They exist so a router can be included in a bare
``FastAPI()`` and driven by a test with no session middleware and no host app;
``web_app.py`` replaces both through ``app.dependency_overrides`` at mount
time, and that mechanism is untouched by this module.

**Sharing the function object is the part to understand before changing
anything here.** ``dependency_overrides`` is a dict keyed by the callable, so
five separate declarations meant five separate keys and ``web_app.py`` set all
five — to the same two values, ``_require_api_auth`` and ``_verify_origin``.
With one object the five assignments collapse to two entries with those same
values, which is why nothing observable moves. What it does mean is that a
future caller cannot override one router's auth and leave another's alone on
the same app. Nothing wants that: the whole point of the stub is that the host
app supplies one answer for every router it mounts, and a router that needs its
own gate declares one of its own (``briefings.require_admin`` is the existing
example, and it stays where it is).

``verify_origin`` returning ``None`` is not "CSRF is off" — it is the seam the
host fills. A router mounted without the override is a router with no CSRF
check, which is correct for a test client and is why ``web_app.py`` sets the
override on the same line it includes the router.

``make_get_user_context`` covers the three routers (health, briefings, feeds)
whose ``get_user_context`` differed only in the module's resolver, its own
``UserNotFoundError``, the ``app.state`` cache attribute and whether
``ensure_initialised`` takes the config. Each call returns a **distinct**
function object, deliberately and unlike the two stubs above: tests override
one module's context per app and must not reach the other two.

``money/routes.py`` has ``get_user_config`` rather than a context and
``garmin_routes.py`` has no per-user resolver at all, so neither takes the
factory. Both take the two stubs.

FastAPI only, no config and no DB, so a router can import it from anywhere in
its own import block — two of the five sit after their module's imports because
that is where the import order puts them, and nothing here depends on going
first.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request


def require_auth(request: Request) -> dict:
    """Return ``{"username": ..., "display_name": ...}`` or raise 401.

    Default reads ``request.session["user"]`` (Starlette SessionMiddleware).
    The host app overrides this with its own auth dependency; the
    ``AssertionError``/``AttributeError`` arm is what a request with no
    SessionMiddleware installed raises.
    """
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        # No SessionMiddleware installed.
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def verify_origin(request: Request) -> None:
    """CSRF check stub for mutating routes — host overrides via dependency_overrides.

    Default is a no-op so the router stays usable in isolation (tests). The host
    app installs a real Origin/Referer check. Same shape as ``require_auth``.
    """
    return None


def make_get_user_context(
    *,
    cache_attr: str,
    resolve: Callable,
    ensure: Callable,
    not_found: type[Exception],
) -> Callable:
    """Build a module router's ``get_user_context`` dependency.

    ``resolve(user_id, istota_config)`` returns the module context or raises
    ``not_found``, which becomes a 404 carrying the exception's own text.
    ``ensure(ctx, istota_config)`` runs the module's first-use initialisation,
    once per ``db_path`` per process: ``init_db`` sets WAL and runs
    ``CREATE TABLE IF NOT EXISTS``, WAL is persistent in the SQLite file header,
    and caching by ``db_path`` also keeps concurrent requests from racing on the
    ``journal_mode`` transition lock. A context that has been initialised
    already still gets ``ensure_dirs()``, because the workspace can be removed
    under a live process while the cache entry survives.

    The cache is a ``set`` on ``app.state`` under ``cache_attr``, per module, so
    two modules sharing a process do not shadow each other's initialisation.

    **``resolve`` and ``not_found`` are bound at import and ``ensure`` is not**,
    which is worth knowing before reaching for a monkeypatch. The first two are
    values passed here, so ``setattr`` on the router module's own
    ``resolve_for_user`` or ``UserNotFoundError`` no longer reaches this; the
    third is called through each router's lambda, which resolves
    ``ensure_initialised`` out of that module's globals at call time and still
    does. Nothing in the tree patches any of the three — the route suites
    override the whole dependency instead, which is the seam that survives
    either way.
    """

    def get_user_context(
        request: Request,
        user: dict = Depends(require_auth),
    ):
        istota_config = getattr(request.app.state, "istota_config", None)
        try:
            ctx = resolve(user["username"], istota_config)
        except not_found as e:
            raise HTTPException(404, str(e))
        cache: set | None = getattr(request.app.state, cache_attr, None)
        if cache is None:
            cache = set()
            setattr(request.app.state, cache_attr, cache)
        if ctx.db_path not in cache:
            ensure(ctx, istota_config)
            cache.add(ctx.db_path)
        else:
            ctx.ensure_dirs()
        return ctx

    return get_user_context
