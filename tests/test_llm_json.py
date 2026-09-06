"""The shared model-output fence rule (F38).

``toml_fence.py`` answers the same question about a file the user writes, and
its test file is the template for this one: the rule lives here, each
caller's own file covers what it does with the answer.
"""

import json
import re
import time

import pytest

from istota.llm_json import (
    FENCE_CLOSE_RE,
    FENCE_OPEN_RE,
    candidate_json_blocks,
    find_fenced_block,
    iter_fenced_blocks,
    iter_opener_delimited_blocks,
    strip_fences,
)


# The two expressions this module replaced, kept verbatim so every claim
# about "the old behaviour" below is executed rather than asserted. They are
# not the same expression and they did not have the same defect, which is the
# thing the first draft of this file got wrong.
OLD_CONTEXT_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)
OLD_HEALTH_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)\n```", re.DOTALL)


def _old(rx, text):
    m = rx.search(text)
    return None if m is None else m.group(1).strip()


def _parses(candidate):
    try:
        json.loads(candidate)
    except ValueError:
        return False
    return True


class TestAMarkerIsALine:
    """Three backticks are a marker only when alone on their line.

    **Which of the two old expressions each case discriminates against is
    stated per test, and that is the point of the class.** The first draft
    asserted the truncation cases against ``find_fenced_block`` alone and
    called them the fix; both reviewers executed the old health expression
    and found it answers those correctly, because its closer needs a ``\\n``
    in front of the run. Only ``context``'s closer is a bare run. Each test
    below now asserts the old answer too, so a case that could not have
    failed says so in its own body.
    """

    def test_a_stray_run_inside_a_string_value_does_not_close_the_block(self):
        text = '```json\n{"note": "run ```make``` first"}\n```\n'
        # context's expression is the one that truncated here.
        assert _old(OLD_CONTEXT_RE, text) == '{"note": "run'
        # The health expression already got this right; the conversion of
        # those three modules rests on the quadratic shape, not on this.
        assert _old(OLD_HEALTH_RE, text) == '{"note": "run ```make``` first"}'
        assert find_fenced_block(text) == '{"note": "run ```make``` first"}'

    def test_a_stray_run_mid_document_does_not_truncate(self):
        text = (
            "Here is what I found.\n\n"
            '```json\n{"a": 1, "b": "see ``` in the log"}\n```\n\n'
            "Let me know if that helps.\n"
        )
        assert _old(OLD_CONTEXT_RE, text) == '{"a": 1, "b": "see'
        assert _old(OLD_HEALTH_RE, text) == '{"a": 1, "b": "see ``` in the log"}'
        assert find_fenced_block(text) == '{"a": 1, "b": "see ``` in the log"}'

    def test_a_line_leading_run_inside_the_body_no_longer_closes_early(self):
        """The one truncation the *health* expression had.

        Its closer is ``\\n`` plus a run, so a nested fence's opener line ends
        the block. Bounded rather than eliminated by the fact that valid JSON
        cannot carry a raw newline in a string, so this cannot happen inside
        a payload that would have parsed — which is why it is a narrowing
        the health sites gain rather than a defect they had.
        """
        text = '```json\n{"a": 1}\n```python\ny = 2\n```\n'
        assert _old(OLD_HEALTH_RE, text) == '{"a": 1}'
        assert find_fenced_block(text) == '{"a": 1}\n```python\ny = 2'

    def test_a_decorated_closer_does_not_close(self):
        text = '```json\n{"a": 1}\n``` done\n'
        assert _old(OLD_HEALTH_RE, text) == '{"a": 1}'
        assert _old(OLD_CONTEXT_RE, text) == '{"a": 1}'
        assert find_fenced_block(text) is None

    def test_prose_sharing_the_opener_line_is_not_an_opener(self):
        text = 'Here: ```json\n{"a": 1}\n```\n'
        assert _old(OLD_HEALTH_RE, text) == '{"a": 1}'
        assert find_fenced_block(text) is None
        # …and this is the narrowing `candidate_json_blocks`' relaxed arm
        # exists to undo, where a wrong block is tried rather than returned.
        assert candidate_json_blocks(text)[0] == '{"a": 1}'


class TestTheBoundsAreLoose:
    """Every bound is looser than CommonMark, for ``toml_fence``'s reason.

    The expressions being replaced had no ``^`` at all, so almost any bound
    is a narrowing and a narrowing breaks input that used to work.
    """

    @pytest.mark.parametrize("label,text", [
        ("no-lang", '```\n{"a": 1}\n```\n'),
        ("json", '```json\n{"a": 1}\n```\n'),
        ("uppercase-lang", '```JSON\n{"a": 1}\n```\n'),
        ("unknown-lang", '```jsonc\n{"a": 1}\n```\n'),
        ("info-string", '```json title="x"\n{"a": 1}\n```\n'),
        ("indent-3", '   ```json\n{"a": 1}\n   ```\n'),
        ("indent-tab", '\t```json\n{"a": 1}\n\t```\n'),
        ("four-backticks", '````json\n{"a": 1}\n````\n'),
        ("mixed-lengths", '````json\n{"a": 1}\n```\n'),
        ("trailing-space", '```json\n{"a": 1}\n```   \n'),
        ("trailing-nbsp", '```json\n{"a": 1}\n```\xa0\n'),
        ("bom", '﻿```json\n{"a": 1}\n```\n'),
        ("no-trailing-newline", '```json\n{"a": 1}\n```'),
    ])
    def test_a_shape_the_old_expression_accepted_is_still_accepted(self, label, text):
        assert find_fenced_block(text) == '{"a": 1}', label

    def test_crlf(self):
        """``$`` under MULTILINE matches before ``\\n``, never before ``\\r``."""
        assert find_fenced_block('```json\r\n{"a": 1}\r\n```\r\n') == '{"a": 1}'

    def test_the_two_markers_agree_on_indent_and_length(self):
        for indent, ticks in (("", "```"), ("   ", "````"), ("\t", "```")):
            assert FENCE_OPEN_RE.match(f"{indent}{ticks}json\n"), (indent, ticks)
            assert FENCE_CLOSE_RE.match(f"{indent}{ticks}\n"), (indent, ticks)


class TestTheSearchIsLinear:
    """The combined ``open(.*?)close`` form is quadratic here.

    Every opener is a fresh start position and each rescans to EOF. Measured
    against the health expression this replaces, on a document of repeated
    openers with no closer: 0.10s at 16 KB, 1.64s at 64 KB and **26.5s at
    256 KB**. The health modules feed it whole OCR responses, so the input
    size is the model's to choose.

    The threshold is 2.0s rather than something tighter because the
    assertion has to be non-flaky on a loaded box; 26.5s against 2.0s is a
    wide enough margin that the loose threshold still discriminates, which
    is the thing a wall-clock assertion usually gets wrong.

    **The fixture is every line an anchored opener, and that is a
    correction.** The first version was ``"```json\\nx" * N``, which holds
    exactly *one* anchored opener — every later line is ``x```json`` — so a
    line-anchored *combined* expression ran it in 0.004s and sailed past the
    2.0s bar. The control that appeared to prove the class (reverting to the
    old combined expression) reverted the anchoring and the two-search shape
    together, and it was the anchoring doing the work. Found by a reviewer,
    measured: an anchored combined expression against the fixture below
    takes 46s, and against the old one 0.004s. As written the class goes red
    under either regression.
    """

    # 256 KB in which every line is an anchored opener and nothing can ever
    # close: the closer wants a backtick run *alone* on its line.
    UNCLOSED_256K = "```json\n" * ((256 * 1024) // 8)

    def test_the_fixture_is_many_anchored_openers_and_no_closer(self):
        """The property that makes the three timing cases discriminating.

        Asserted rather than assumed, because the first fixture looked
        identical and held one opener.
        """
        assert len(self.UNCLOSED_256K) > 256 * 1000
        assert len(FENCE_OPEN_RE.findall(self.UNCLOSED_256K)) > 10000
        assert FENCE_CLOSE_RE.search(self.UNCLOSED_256K) is None

    def test_find_returns_quickly(self):
        assert len(self.UNCLOSED_256K) > 256 * 1000
        started = time.monotonic()
        assert find_fenced_block(self.UNCLOSED_256K) is None
        assert time.monotonic() - started < 2.0

    def test_iterating_every_block_returns_quickly(self):
        started = time.monotonic()
        assert list(iter_fenced_blocks(self.UNCLOSED_256K)) == []
        assert time.monotonic() - started < 2.0

    def test_the_health_entry_point_returns_quickly(self):
        """``candidate_json_blocks`` is the one the OCR modules call."""
        started = time.monotonic()
        candidate_json_blocks(self.UNCLOSED_256K)
        assert time.monotonic() - started < 2.0

    def test_many_closed_blocks_are_also_linear(self):
        text = '```json\n{"a": 1}\n```\n' * 20000
        started = time.monotonic()
        assert len(list(iter_fenced_blocks(text))) == 20000
        assert time.monotonic() - started < 2.0


class TestIterFencedBlocks:
    def test_blocks_come_back_in_document_order(self):
        text = "```\nfirst\n```\nprose\n```json\nsecond\n```\n"
        assert list(iter_fenced_blocks(text)) == ["first", "second"]

    def test_a_lang_filter_selects(self):
        text = "```python\nnot this\n```\n```json\nthis\n```\n"
        assert list(iter_fenced_blocks(text, lang="json")) == ["this"]

    def test_the_lang_filter_is_case_insensitive(self):
        assert list(iter_fenced_blocks("```JSON\nx\n```\n", lang="json")) == ["x"]

    def test_no_fence_at_all(self):
        assert list(iter_fenced_blocks('{"a": 1}')) == []
        assert find_fenced_block('{"a": 1}') is None

    def test_an_empty_block(self):
        assert list(iter_fenced_blocks("```json\n```\n")) == [""]


class TestStripFences:
    """Replaces ``health/explainer._strip_fences`` and

    ``memory.curation.prompt.strip_json_fences``. Every input either copy
    accepted still gives the same answer.
    """

    @pytest.mark.parametrize("text,expected", [
        ('```json\n{"ops": []}\n```', '{"ops": []}'),
        ('```\n{"ops": []}\n```', '{"ops": []}'),
        ('{"ops": []}', '{"ops": []}'),
        ('  \n```json\n{"ops": []}\n```\n  ', '{"ops": []}'),
    ])
    def test_the_four_cases_the_curation_suite_already_pinned(self, text, expected):
        assert strip_fences(text) == expected

    def test_an_opener_with_no_closer_still_yields_the_body(self):
        """A truncated model response is an ordinary event.

        Both replaced copies dropped the opener line and returned the rest.
        """
        assert strip_fences('```json\n{"ops": []}') == '{"ops": []}'

    def test_a_trailing_run_not_on_its_own_line_is_still_trimmed(self):
        """The tail is looser than the head, on purpose.

        An unanchored closer at the very end of a string that already began
        with a fence can truncate nothing, so tightening it would only lose
        parses that used to work.
        """
        assert strip_fences('```json\n{"ops": []}```') == '{"ops": []}'

    def test_a_degenerate_single_line_fence(self):
        assert strip_fences('```{"ops": []}```') == '{"ops": []}'

    def test_a_fence_that_does_not_start_the_text_is_left_alone(self):
        """Both copies checked ``startswith``; the prose is what was asked for."""
        text = 'Sure:\n```json\n{"ops": []}\n```'
        assert strip_fences(text) == text

    def test_an_info_string_with_a_space_is_now_handled(self):
        """``explainer``'s ``^```[a-zA-Z]*\\n`` left this opener in place.

        A widening: the fence line used to survive into ``json.loads``.
        """
        assert strip_fences('```json title="x"\n{"ops": []}\n```') == '{"ops": []}'


class TestCandidateJsonBlocks:
    """The health OCR entry point. Order and de-duplication are the contract."""

    def test_fenced_blocks_come_first(self):
        raw = 'Here you go:\n```json\n{"biomarkers": []}\n```\n'
        assert candidate_json_blocks(raw)[0] == '{"biomarkers": []}'

    def test_the_whole_text_then_the_widest_object_then_the_widest_array(self):
        raw = 'prose {"a": 1} more [1, 2] tail'
        got = candidate_json_blocks(raw)
        assert got == [raw.strip(), '{"a": 1}', "[1, 2]"]

    def test_duplicates_collapse_and_order_is_kept(self):
        raw = '{"a": 1}'
        assert candidate_json_blocks(raw) == ['{"a": 1}']

    def test_empty_candidates_are_dropped(self):
        assert candidate_json_blocks("") == []

    def test_the_strict_arm_comes_first_because_the_relaxed_one_can_be_stolen(self):
        """Why both arms, and why in this order.

        A backtick run mentioned in the prose steals the relaxed opener, and
        the block then runs from that mention to the real fence's closer —
        swallowing the ``` ```json ``` line and parsing as nothing. The strict
        arm is what reads it correctly, so it has to be tried first.

        This is a pin rather than an observation: with only the relaxed arm
        the whole class still passed, measured. A differential over 400k
        random inputs put the strict arm's unique contribution at 0.76%, and
        this is what that looks like in prose a model would actually write.
        """
        raw = (
            "Wrap it in ``` when you paste it back.\n\n"
            "```json\n"
            '{"biomarkers": [{"name": "HGB"}], "lab_name": "Acme Labs"}\n'
            "```\n"
        )
        strict = list(iter_fenced_blocks(raw))
        relaxed = list(iter_fenced_blocks(raw, relaxed=True))
        assert strict == [
            '{"biomarkers": [{"name": "HGB"}], "lab_name": "Acme Labs"}'
        ]
        assert not _parses(relaxed[0])
        assert candidate_json_blocks(raw)[0] == strict[0]

    def test_an_opener_sharing_its_line_with_prose_is_still_read(self):
        """The relaxed arm. ``find_fenced_block`` declines this one.

        Anchoring the opener buys nothing against F38's defect, which is a
        block ending *early*, and it costs a shape models emit.
        """
        raw = 'Here it is: ```json\n{"biomarkers": [1]}\n```'
        assert find_fenced_block(raw) is None
        assert candidate_json_blocks(raw)[0] == '{"biomarkers": [1]}'

    def test_the_relaxed_arm_is_what_stops_a_silently_different_answer(self):
        """The measured regression the relaxed arm was added for.

        With prose carrying a ``{`` in front of the fence, the
        widest-``{...}`` span reaches from that brace into the JSON and is
        invalid, and the widest-``[...]`` arm then answers with the *inner*
        array — which ``ocr._parse_llm_response`` accepts through its
        bare-list branch, returning a panel that has silently lost
        ``drawn_at``, ``lab_name`` and ``panel_type``. A lost parse is
        visible; this one is not.
        """
        raw = (
            "The row {x} was unclear. Here: ```json\n"
            '{"biomarkers": [{"name": "HGB"}], "lab_name": "Acme Labs"}\n```'
        )
        first = candidate_json_blocks(raw)[0]
        assert json.loads(first)["lab_name"] == "Acme Labs"

    def test_the_relaxed_arm_does_not_reopen_the_defect_it_sits_beside(self):
        """A stray run inside the JSON must still not close the block.

        The relaxed arm drops the anchor from the opener only. Were it
        dropped from the closer too, this block would end at ``` ```make ```
        and the first candidate would be a truncated fragment.
        """
        raw = '```json\n{"note": "run ```make``` first", "biomarkers": []}\n```'
        assert candidate_json_blocks(raw)[0] == (
            '{"note": "run ```make``` first", "biomarkers": []}'
        )

    def test_a_decorated_closer_stays_unread_and_falls_to_the_fallback(self):
        """That one is F38's actual fix, so the relaxed arm must not undo it."""
        raw = '```json\n{"biomarkers": [1]}\n``` done'
        assert list(iter_fenced_blocks(raw, relaxed=True)) == []
        assert '{"biomarkers": [1]}' in candidate_json_blocks(raw)

    def test_a_forgotten_closer_before_a_second_opener_still_yields_a_block(self):
        """The other shape the old ``finditer`` reached.

        Its closer was any ``\\n``-led run, and a following opener *line* is
        one, so it recovered the first block. The closer-based walk does not:
        the second ``` ```json ``` line is not a closer, so the block runs to
        the final one and parses as nothing, and both widest arms then span
        the whole malformed pair. ``iter_opener_delimited_blocks`` is the arm
        for it.
        """
        raw = (
            '```json\n{"biomarkers": [{"name": "HDL"}]}\n'
            '```json\n{"biomarkers": [{"name": "LDL"}]}\n```'
        )
        assert list(iter_fenced_blocks(raw)) != ['{"biomarkers": [{"name": "HDL"}]}']
        assert list(iter_opener_delimited_blocks(raw))[0] == (
            '{"biomarkers": [{"name": "HDL"}]}'
        )
        got = candidate_json_blocks(raw)
        parsed = next(
            (json.loads(c) for c in got if _parses(c)), None,
        )
        assert parsed == {"biomarkers": [{"name": "HDL"}]}

    def test_the_accepted_loss_is_named_rather_than_implied(self):
        """A decorated closer whose trailing prose carries a bracket.

        No arm yields a parseable candidate — not a fragment, nothing. The
        OCR modules return their empty payload. Recorded here so the next
        change to the fallbacks sees the boundary rather than rediscovering
        it, and because the old expression did parse this one.
        """
        raw = (
            '```json\n{"biomarkers": [{"name": "HGB"}], "lab_name": "Acme"}\n'
            '``` done, see [1] and {x}'
        )
        assert _old(OLD_HEALTH_RE, raw) is not None
        assert not any(_parses(c) for c in candidate_json_blocks(raw))

    def test_the_relaxed_walk_is_linear_too(self):
        started = time.monotonic()
        assert list(
            iter_fenced_blocks(TestTheSearchIsLinear.UNCLOSED_256K, relaxed=True)
        ) == []
        assert time.monotonic() - started < 2.0


class TestThereIsOneCopy:
    """The pin: a fourth ``_FENCE_RE`` must fail this."""

    def test_no_module_outside_llm_json_carries_the_health_fence_expression(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "istota"
        needle = r"```(?:[a-zA-Z]+)?"
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            # llm_json quotes the expression it replaced, in its docstring.
            if p.name != "llm_json.py"
            and needle in p.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            "these modules re-implement the model-output fence; call "
            "llm_json.candidate_json_blocks or find_fenced_block instead"
        )

    def test_the_only_unanchored_fence_left_is_the_one_that_names_its_reason(self):
        """``session/result`` removes fenced blocks rather than unwrapping one.

        It is exempt because line-anchoring it would flag an inline
        ```` ```<invoke``` ```` as a malformed result, and it is not the
        quadratic shape — its closer is a bare backtick run. Its own comment
        says both. Any *other* file matching this is a new copy.
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "istota"
        offenders = []
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root))
            if rel in ("llm_json.py", "session/result.py"):
                continue
            text = path.read_text(encoding="utf-8")
            # Across the newline: this repo writes `re.compile(\n    r"..."`
            # as often as it writes it on one line, and a fourth copy would
            # most likely be written the same way. The first version of this
            # needle was adjacent-characters-only and would have missed it.
            if re.search(r"compile\(\s*r?f?[\"']`{3}", text):
                offenders.append(rel)
        assert offenders == []
