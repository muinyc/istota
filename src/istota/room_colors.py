"""The room-colour palette: which names a room may be tinted with.

ISSUE-433. A room carries a colour so the chat sidebar can be scanned by
recognition rather than read. The value stored is a **name** from this fixed
list, never a colour — `web/src/lib/styles/tokens.css` resolves each name to a
token pair, one value per theme.

**A fixed palette rather than a picker, and that is the design decision rather
than a limitation.** `web/AGENTS.md` requires every meaning-bearing colour to
be defined in both theme blocks, because the surfaces a row tint sits on are
`--surface-raised` — `#222` in dark and `#e8e8ea` in light. One user-picked
value cannot read on both: on one theme it is invisible and on the other it is
a block of colour. Nor would the design linter catch it, since `raw-color`
scans source text and a hex arriving from the database is not source text — the
lint would pass while its whole intent was defeated. So the palette is ours and
the choice among it is the user's, which is the carve-out `web/AGENTS.md`
already makes for "categorical palettes, where the hue encodes a *kind* rather
than a severity".

**Names only, deliberately.** `location-constants.ts` is the other categorical
palette in the tree and it holds hex, because MapLibre paints in WebGL and
cannot resolve `var(--token)`. Nothing here has that problem, so the colour
values live in `tokens.css` alone and both this module and `roomColors.ts`
carry names. `tests/test_room_colors.py` is what holds the three in step.

Order is the order the picker renders, so it is part of the interface rather
than incidental: the hues run round the wheel, which is what makes two
adjacent swatches obviously different.

stdlib-only leaf, imports nothing — the validation site is a web route, but a
palette is not a web concept and a test importing `web_app` to read eight
strings would pull in the whole application.
"""

# The amber band is skipped on purpose. `.room-origin.talk` already spends
# `--accent-amber` in this exact row to mean "mirrored to Nextcloud Talk", so a
# room colour landing there would make a user's tint read as a surface binding.
ROOM_COLORS: tuple[str, ...] = (
    "rose",
    "coral",
    "citron",
    "green",
    "teal",
    "sky",
    "indigo",
    "plum",
)


def is_room_color(value: object) -> bool:
    """Whether `value` names a palette entry.

    Takes `object` rather than `str`: the caller is a request body, so the
    value is whatever JSON carried. Clearing is the empty string or `None` and
    is **not** this function's business — a caller distinguishes "clear it"
    from "set it" before asking, the way the route does.
    """
    return isinstance(value, str) and value in ROOM_COLORS
