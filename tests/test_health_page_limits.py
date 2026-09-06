"""The Documents view's page sizes must fit the caps the routes declare.

``PAGE_SIZES`` (``web/src/lib/health/documents.ts``) is what the Documents view
asks each of its four lists for; every ``limit`` there is validated against an
``le`` on the route. FastAPI rejects an over-cap value with a 422 *before* the
handler runs, so nothing clamps it and nothing degrades — and the four reads sit
in one ``Promise.all``, so a single rejection blanks the whole page with
"Health API error: 422" however few documents are stored.

That is what shipped: one shared page size of 1000 against ``/encounters`` and
``/diagnoses``, both capped at 500. The numbers now differ per list, which means
they can drift, and this is the only thing that would notice — the browser is
the only caller, and the default suite never issues the request.

Same shape and the same reason as ``tests/test_feeds_media_parity.py``: parsed
with a regex rather than executed, so it needs no node.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from istota.health.routes import router

DOCUMENTS_TS = (
    Path(__file__).resolve().parents[1]
    / "web" / "src" / "lib" / "health" / "documents.ts"
)

# Which route each key of PAGE_SIZES pages through. Kept here rather than
# derived from the key, so renaming a key to something that happens not to be a
# path fails loudly instead of silently skipping that list.
ROUTE_FOR_LIST = {
    "documents": "/documents",
    "encounters": "/encounters",
    "diagnoses": "/diagnoses",
    "immunizations": "/immunizations",
}


def _client_page_sizes() -> dict[str, int]:
    """Extract ``PAGE_SIZES`` as ``{list: size}``."""
    source = DOCUMENTS_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export const PAGE_SIZES = \{(.*?)\n\} as const;",
        source,
        re.DOTALL,
    )
    assert match, "PAGE_SIZES not found in documents.ts — did it get renamed?"
    body = match.group(1)
    sizes = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+),", body)}
    assert sizes, f"PAGE_SIZES parsed empty from: {body!r}"
    return sizes


def _route_limit_caps() -> dict[str, int]:
    """The ``le`` on each GET list route's ``limit`` query parameter."""
    caps: dict[str, int] = {}
    for route in router.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        for param in route.dependant.query_params:
            if param.name != "limit":
                continue
            for constraint in param.field_info.metadata:
                le = getattr(constraint, "le", None)
                if le is not None:
                    caps[route.path] = int(le)
    return caps


def test_every_page_size_fits_its_route_cap():
    caps = _route_limit_caps()
    for name, size in _client_page_sizes().items():
        path = ROUTE_FOR_LIST.get(name)
        assert path is not None, (
            f"PAGE_SIZES.{name} pages through no route this test knows about; "
            "add it to ROUTE_FOR_LIST"
        )
        assert path in caps, f"GET {path} declares no le on its limit parameter"
        assert size <= caps[path], (
            f"PAGE_SIZES.{name} asks GET {path} for {size}, over its cap of "
            f"{caps[path]} — that request is a 422 and the Documents view "
            "fails whole"
        )


def test_the_map_covers_every_list_the_view_reads():
    assert set(_client_page_sizes()) == set(ROUTE_FOR_LIST)
