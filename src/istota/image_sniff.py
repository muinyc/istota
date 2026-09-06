"""Which bytes istota will render as an image on its own origin.

`/chat/files` serves a file out of the caller's own workspace, and that
workspace holds user- and model-authored HTML and SVG. Serving those inline
would execute them on the app's origin against the session cookie that just
authorized the read, which is why every response there was `attachment`. The
narrow exception is a raster: a response carrying an explicit
`Content-Type: image/png` derived from a PNG signature, plus
`X-Content-Type-Options: nosniff`, cannot be reinterpreted as a document by any
browser. A file that is both a valid PNG and valid HTML is served as
`image/png` and stays an image.

**Decided from the first bytes, never from the name.** The extension is a
caller-supplied string on a file the model wrote; `avatars.py` already refuses
to trust one. An SVG named `.png` is the case that settles it, and it is a
test.

**Four formats**, matching what `avatars.ACCEPTED_FORMATS` admits minus HEIF.
HEIF is left out deliberately: browser support is not universal, and an inline
type that does not draw is worse than an attachment that does. SVG is XML text
and matches no signature here, which is the point of sniffing rather than
mapping a suffix.

**No decode.** A magic-number test must not open the file with an image
library: `web_app.py` already runs its avatar decode on a serialized
single-worker executor because Pillow's peak memory is not bounded by the byte
cap it enforces, and a download route must not join that queue.

A leaf rather than a function inside `web_app.py`, so the skill side can hold
the same predicate about what counts as an inline-servable image without a
second copy of the table. stdlib-only, imports nothing, never raises — the
caller is a download route, where a traceback is a 500 on a file the user owns.
"""

from __future__ import annotations

__all__ = ["INLINE_MEDIA_TYPES", "SNIFF_BYTES", "sniff_raster"]


INLINE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# What a caller has to read off the front of the file. WebP needs 12 (`RIFF` at
# 0 and `WEBP` at 8); the rest need 8 or fewer. Rounded up, so a caller reading
# this many bytes never has to change when a format is added.
SNIFF_BYTES: int = 32

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
_RIFF_SIGNATURE = b"RIFF"
_WEBP_FORM_TYPE = b"WEBP"


def sniff_raster(head: object) -> str | None:
    """The media type of a raster istota will render inline, or None.

    `head` is the first bytes of the file — at least `SNIFF_BYTES` where the
    file is that long, fewer where it is not. Any signature that does not match
    at offset zero is None: a leading space before a JPEG signature is a miss,
    because a browser given `image/jpeg` would not draw it either.

    **`object` rather than `bytes`, deliberately.** The module's contract is
    that it never raises, and a `bytes`-annotated parameter is a promise the
    type checker keeps and the runtime does not — `None.startswith` is an
    `AttributeError`, which on this caller is a 500 on a file the user owns.
    The annotation is the only thing standing between a future caller's
    `Optional[bytes]` and that, so the guard is the contract rather than
    defensive padding. It also takes `bytearray` and `memoryview`, which is
    what a caller reading into a preallocated buffer hands back — the same
    reasoning `kv_namespaces.is_reserved_namespace` and the `surfaces.py`
    readers state for their own widened signatures.
    """
    if isinstance(head, memoryview):
        # `bytes(mv)` on a view whose itemsize is not 1 *reinterprets* the
        # underlying buffer rather than raising, so a view of an `array("I")`
        # whose leading four bytes happen to match would be answered as an
        # image. A non-contiguous view is not the file's leading bytes either.
        if head.itemsize != 1 or not head.c_contiguous:
            return None
        head = head.tobytes()
    elif isinstance(head, bytearray):
        head = bytes(head)
    if not isinstance(head, bytes):
        return None

    if head.startswith(_PNG_SIGNATURE):
        return INLINE_MEDIA_TYPES["png"]
    if head.startswith(_JPEG_SIGNATURE):
        return INLINE_MEDIA_TYPES["jpeg"]
    if head.startswith(_GIF_SIGNATURES):
        return INLINE_MEDIA_TYPES["gif"]
    # A RIFF container holds WAVE and AVI too, so the form type at offset 8 is
    # the half that says it is an image.
    if head.startswith(_RIFF_SIGNATURE) and head[8:12] == _WEBP_FORM_TYPE:
        return INLINE_MEDIA_TYPES["webp"]
    return None
