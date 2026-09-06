"""What counts as a raster istota will render on its own origin.

The predicate is the whole of the security argument behind serving a workspace
file `inline` instead of `attachment`, so the cases that matter most are the
misses: an SVG, an HTML document and anything that only *looks* like an image
because of where it sits in a filename.
"""

import pytest

from istota.image_sniff import INLINE_MEDIA_TYPES, SNIFF_BYTES, sniff_raster

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
GIF87 = b"GIF87a\x01\x00\x01\x00\x80\x00\x00"
GIF89 = b"GIF89a\x01\x00\x01\x00\x80\x00\x00"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00"


HITS = [
    ("png", PNG, "image/png"),
    ("jpeg", JPEG, "image/jpeg"),
    ("jpeg minimal", b"\xff\xd8\xff", "image/jpeg"),
    ("gif87a", GIF87, "image/gif"),
    ("gif89a", GIF89, "image/gif"),
    ("webp", WEBP, "image/webp"),
]

MISSES = [
    ("empty", b""),
    ("truncated png signature", b"\x89PNG\r\n"),
    ("png signature with a wrong byte", b"\x89PNG\r\n\x1a\x0a"[:7] + b"\x00"),
    ("jpeg preceded by whitespace", b"  \xff\xd8\xff\xe0"),
    ("jpeg two bytes only", b"\xff\xd8"),
    ("riff container that is not webp", b"RIFF\x24\x00\x00\x00WAVEfmt "),
    ("riff truncated before the form type", b"RIFF\x24\x00\x00\x00WEB"),
    ("svg document", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
    ("svg with an xml declaration", b'<?xml version="1.0"?>\n<svg xmlns="http://ww'),
    ("html starting with a comment", b"<!-- hi --><html><body>x</body></html>"),
    ("html doctype", b"<!DOCTYPE html>\n<html>"),
    ("plain text", b"a,b\n1,2\n"),
    ("gif with the wrong version", b"GIF88a\x01\x00\x01\x00"),
    ("bmp", b"BM\x36\x00\x00\x00\x00\x00\x00\x00"),
    ("tiff little endian", b"II\x2a\x00\x08\x00\x00\x00"),
    ("pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
    ("heif", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"),
]


@pytest.mark.parametrize("name,head,expected", HITS, ids=[c[0] for c in HITS])
def test_admitted_signatures(name, head, expected):
    assert sniff_raster(head) == expected


@pytest.mark.parametrize("name,head", MISSES, ids=[c[0] for c in MISSES])
def test_everything_else_misses(name, head):
    assert sniff_raster(head) is None


def test_a_polyglot_is_decided_by_its_first_bytes():
    """A file that is a valid PNG and also parses as HTML is a PNG. The header
    is what makes that safe: an explicit `image/png` plus `nosniff` leaves the
    browser no route to reinterpret it as a document."""
    assert sniff_raster(PNG + b"<script>alert(1)</script>") == "image/png"


def test_html_carrying_a_png_signature_later_is_not_an_image():
    assert sniff_raster(b"<html>" + PNG) is None


def test_a_head_shorter_than_sniff_bytes_is_still_decided():
    """The route reads at most SNIFF_BYTES; a small file yields fewer."""
    assert sniff_raster(PNG[:8]) == "image/png"
    assert sniff_raster(WEBP[:12]) == "image/webp"


def test_extra_bytes_past_the_signature_change_nothing():
    assert sniff_raster(PNG + b"\x00" * 4096) == "image/png"


def test_sniff_bytes_covers_every_signature():
    """SNIFF_BYTES is what the caller reads, so it has to be at least as long
    as the longest signature — WebP's, which needs 12."""
    assert SNIFF_BYTES >= 12
    for _name, head, expected in HITS:
        assert sniff_raster(head[:SNIFF_BYTES]) == expected


def test_media_types_are_the_four_declared():
    assert set(INLINE_MEDIA_TYPES.values()) == {
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }


def test_svg_is_not_in_the_inline_set():
    """SVG is a script-bearing document. It is XML text, so it matches no
    signature — which is the point, not an accident of the table."""
    assert "image/svg+xml" not in INLINE_MEDIA_TYPES.values()


@pytest.mark.parametrize(
    "bad", [None, "\x89PNG\r\n\x1a\n", 42, [], {}],
    ids=["none", "str", "int", "list", "dict"],
)
def test_never_raises_on_a_non_bytes_head(bad):
    """A leaf that never raises: the caller is a download route, and a
    traceback there is a 500 on a file the user owns."""
    assert sniff_raster(bad) is None


def test_a_bytearray_and_a_memoryview_are_accepted():
    """A caller reading from a file may hand back either."""
    assert sniff_raster(bytearray(PNG)) == "image/png"
    assert sniff_raster(memoryview(PNG)) == "image/png"


def test_a_memoryview_that_is_not_a_byte_view_is_refused():
    """`bytes(mv)` reinterprets rather than raising when itemsize is not 1, so
    a view over wider items whose leading bytes happen to match would otherwise
    be answered as an image."""
    import array

    buf = array.array("I", [0x474E5089, 0x0A1A0A0D])
    wide = memoryview(buf)
    assert wide.itemsize != 1
    # The reinterpretation is real — this is what the guard is refusing.
    assert bytes(wide).startswith(b"\x89PNG\r\n\x1a\n")
    assert sniff_raster(wide) is None
    # Cast back to bytes and it is admitted, so the guard is about the view's
    # shape rather than about the buffer.
    assert sniff_raster(wide.cast("B")) == "image/png"


def test_a_non_contiguous_memoryview_is_refused():
    strided = memoryview(bytearray(PNG + PNG))[::2]
    assert not strided.c_contiguous
    assert sniff_raster(strided) is None
