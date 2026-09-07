"""Prompt construction for op-based USER.md curation, plus JSON-fence stripping."""

from __future__ import annotations

from collections.abc import Sequence

from ...llm_json import strip_fences
from .parser import serialize_sectioned_doc
from .types import SectionedDoc

#: Bounds on one inventory row. A skill name is a directory name in the
#: operator's own tree and a count is a `len()`, so both are already small;
#: these stop a future caller's unbounded value from filling the prompt.
_MAX_NAME_CHARS = 60
_MAX_LINES = 100_000


def render_skill_overlay_inventory(
    overlays: Sequence[tuple[str, int]] | None,
) -> str:
    """One bullet per overlay: skill name and line count, never the body.

    The curator is told a topic is handled elsewhere so it does not re-add the
    notes conventions to USER.md a week after they moved out of it. It is not
    asked to read those rules again, so no body is rendered — and it does not
    write overlays, so no op names one.

    A malformed row is dropped rather than raising: this runs on the nightly
    path, where the alternative to a missing bullet is a curation pass that
    does not happen at all. Today's caller has already required each name to be
    a *known* skill and takes each count from a `len()`, so the checks here are
    for the caller that has not: a name carrying a newline would forge a bullet
    or a section, and `int()` alone accepts a float that raises `OverflowError`
    on the way in (not caught by `ValueError`) and an integer of any width on
    the way out. A row must be a pair, the count a plain non-negative `int`,
    and the name one line of printable text.
    """
    rows: list[str] = []
    for entry in overlays or ():
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            continue
        name, count = entry
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        clean = " ".join(
            "".join(c for c in str(name) if c.isprintable() or c.isspace()).split()
        )
        if not clean:
            continue
        shown = min(count, _MAX_LINES)
        rows.append(
            f"- {clean[:_MAX_NAME_CHARS]}: {shown} line{'' if shown == 1 else 's'}"
        )
    return "\n".join(rows)


def build_op_curation_prompt(
    user_id: str,
    doc: SectionedDoc,
    dated_memories: str,
    kg_facts_text: str | None,
    skill_overlays: Sequence[tuple[str, int]] | None = None,
) -> str:
    parts: list[str] = []

    parts.append(
        f"You are curating the durable memory file USER.md for user '{user_id}'.\n"
        "\n"
        "USER.md is the slow tier of memory: small, deliberate, almost append-only.\n"
        "Your job is to emit a JSON list of small operations — never to rewrite the file."
    )

    parts.append("## Current USER.md structure")
    serialized = serialize_sectioned_doc(doc)
    parts.append(serialized.rstrip("\n") if serialized else "(empty)")

    parts.append("## Recent dated memories (3 day window)")
    parts.append(dated_memories.rstrip("\n") if dated_memories else "(none)")

    if kg_facts_text and kg_facts_text.strip():
        parts.append("## Knowledge graph (already stored — do not duplicate to USER.md)")
        parts.append(kg_facts_text.rstrip("\n"))

    overlay_rows = render_skill_overlay_inventory(skill_overlays)
    if overlay_rows:
        parts.append(
            "## Skill overlays (already stored — do not duplicate to USER.md)\n"
            "\n"
            "The user keeps some rules in a file of their own per skill, loaded with\n"
            "that skill. Below are the file names and their sizes. Their contents are\n"
            "deliberately not shown, and the ops below cannot edit them.\n"
            "\n"
            "This list is a reason not to ADD a rule the user has clearly filed under a\n"
            "skill. It is never grounds to REMOVE anything from USER.md: you cannot see\n"
            "what these files say, so you cannot know a bullet is covered by one."
        )
        parts.append(overlay_rows)

    parts.append(
        "## Operations available\n"
        "\n"
        "- append: add a bullet under an EXISTING heading (optionally under one of its\n"
        '  `### subheadings` via "subheading")\n'
        "- add_heading: create a NEW heading with one or more bullets\n"
        "- remove: remove a bullet (heading + substring match; must be unique)\n"
        "- replace: rewrite the single matching bullet in place (heading + match + new line)\n"
        "- remove_heading: drop a whole `## ` section (heading)"
    )

    parts.append(
        "## How ops are applied\n"
        "\n"
        "- Headings are matched **case-sensitive exact** against the structure shown above.\n"
        "  Copy the heading text verbatim.\n"
        '- "Bullet" means a line starting with `-`, `*`, or `1.` (etc). Paragraphs and `### subheading`\n'
        "  lines themselves are NOT bullets and are NEVER matched or removed by these ops.\n"
        "- `remove` and `replace` match `match` as a case-insensitive substring against bullet text\n"
        "  across the WHOLE section (top region AND `### subsections`). If zero bullets match, the op\n"
        "  is a quiet no-op. If multiple match, the op is rejected — be more specific.\n"
        "- `append` without a subheading inserts at the end of the top region (before any `###`).\n"
        '  With "subheading" set, it appends under that subsection instead.\n'
        "- `append` deduplicates: identical bullet text in the target region produces no change.\n"
        "- `replace` preserves the matched bullet's indentation; an identical rewrite is a no-op.\n"
        "- `remove_heading` deletes the entire section — use it only for sections that are wholly stale.\n"
        "- `add_heading` rejects existing names; use `append` to add a bullet under an existing heading."
    )

    parts.append(
        "## Rules\n"
        "\n"
        "1. Only emit ops for DURABLE facts: long-lived preferences, projects, people, decisions.\n"
        "2. Skip anything in the knowledge graph above — it is already stored.\n"
        '3. Skip temporary or time-bound info ("meeting tomorrow", "ordered groceries").\n'
        "4. Skip task references (ref:NNNN).\n"
        '5. Most nights, the right answer is `{"ops": []}` — do not invent edits to seem useful.\n'
        "6. To remove an outdated entry, it must be clearly contradicted by newer information. If unsure,\n"
        "   leave it.\n"
        "7. For `remove`, the `match` substring must be specific enough that only ONE line matches."
    )

    parts.append(
        "## Output format\n"
        "\n"
        "Return ONLY a JSON object, no preamble:\n"
        "\n"
        '{"ops": [\n'
        '  {"op": "append", "heading": "Preferences", "line": "..."},\n'
        "  ...\n"
        "]}\n"
        "\n"
        'If nothing to change: {"ops": []}'
    )

    return "\n\n".join(parts) + "\n"


def strip_json_fences(text: str) -> str:
    """Strip ` ```json … ``` ` or ` ``` … ``` ` wrapping if present.

    Kept under this name because it is part of ``memory.curation``'s public
    surface (``__init__`` re-exports it and ``sleep_cycle`` imports it from
    there). The rule itself is :func:`istota.llm_json.strip_fences`, which
    the health explainer's own copy of this also became.
    """
    return strip_fences(text)
