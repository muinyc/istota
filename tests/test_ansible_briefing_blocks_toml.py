"""The Ansible ``istota_briefing_blocks_toml`` filter renders valid, seedable TOML.

Config-authored briefing blocks are in-memory-only Config; the Ansible path
provisions schedule/delivery via the CLI/DB but must render a content-only
``[[users.X.briefings]]`` stub so the blocks reach the module-DB seeder. This
verifies the filter's output parses as TOML and normalises to the expected
seeder specs.
"""

import importlib.util
import tomllib
from pathlib import Path


from istota.briefings import normalize_block_specs
from istota.config import _parse_user_data


_FILTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "ansible" / "filter_plugins" / "istota_toml.py"
)


def _load_filter_module():
    spec = importlib.util.spec_from_file_location("istota_toml", _FILTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_FILTER_MOD = _load_filter_module()
render = _FILTER_MOD.istota_briefing_blocks_toml
render_defaults = _FILTER_MOD.istota_default_briefings_toml


_USERS = {
    "dana": {
        "briefings": [
            {  # blocks-bearing → rendered
                "name": "world",
                "cron": "0 7 * * *",
                "output": "email",
                "blocks": [
                    {
                        "title": "World News",
                        "directive": "3-5 stories, neutral.",
                        "render_mode": "synthesis",
                        "options": {"story_count": 5},
                        "sources": [
                            {"kind": "rss", "config": {
                                "feed_ref": {"kind": "category", "value": 4},
                                "lookback_hours": 24,
                            }},
                            {"kind": "browse", "config": {"preset": "ap"}},
                        ],
                    },
                    {
                        "title": "Markets",
                        "render_mode": "structured",
                        "sources": [{"kind": "markets", "config": {}}],
                    },
                ],
            },
            {  # components-only → NOT rendered by this filter
                "name": "plain",
                "cron": "0 8 * * *",
                "components": {"news": True},
            },
        ]
    }
}


class TestBriefingBlocksTomlFilter:
    def test_empty_when_no_blocks(self):
        assert render({}) == ""
        assert render({"u": {"briefings": [{"name": "x", "cron": "* * * * *"}]}}) == ""
        assert render("not a dict") == ""

    def test_renders_parseable_toml(self):
        out = render(_USERS)
        parsed = tomllib.loads(out)
        briefings = parsed["users"]["dana"]["briefings"]
        # Only the blocks-bearing briefing is rendered.
        assert [b["name"] for b in briefings] == ["world"]
        blocks = briefings[0]["blocks"]
        assert [b["title"] for b in blocks] == ["World News", "Markets"]
        assert blocks[0]["options"] == {"story_count": 5}
        assert blocks[0]["sources"][0]["config"]["feed_ref"] == {
            "kind": "category", "value": 4,
        }

    def test_round_trips_through_parse_and_normalize(self):
        out = render(_USERS)
        parsed = tomllib.loads(out)
        uc = _parse_user_data(parsed["users"]["dana"], "dana")
        # The single rendered briefing carries the raw blocks passthrough.
        assert len(uc.briefings) == 1
        specs = normalize_block_specs(uc.briefings[0].blocks, briefing_name="world")
        assert [s["title"] for s in specs] == ["World News", "Markets"]
        assert [s["kind"] for s in specs[0]["sources"]] == ["rss", "browse"]
        assert specs[1]["render_mode"] == "structured"

    def test_string_escaping(self):
        users = {"u": {"briefings": [{
            "name": "q", "cron": "* * * * *",
            "blocks": [{
                "title": 'Say "hi"\\bye',
                "sources": [{"kind": "notes", "config": {}}],
            }],
        }]}}
        parsed = tomllib.loads(render(users))
        title = parsed["users"]["u"]["briefings"][0]["blocks"][0]["title"]
        assert title == 'Say "hi"\\bye'

    def test_a_multi_line_directive_still_renders_valid_toml(self):
        """A block `directive` is prose written for the model, so a newline in
        it is ordinary rather than hostile — and the escaper handled only `"`
        and `\\`, so it rendered a real newline inside a basic string and the
        whole config stopped parsing. Found while fixing the same gap in
        `config.toml.j2` (ISSUE-443), which shares this escaper.
        """
        directive = "Summarise the week.\nKeep it short.\tNo bullets."
        users = {"u": {"briefings": [{
            "name": "q", "cron": "* * * * *",
            "blocks": [{
                "title": "weekly",
                "directive": directive,
                "sources": [{"kind": "notes", "config": {}}],
            }],
        }]}}
        parsed = tomllib.loads(render(users))
        block = parsed["users"]["u"]["briefings"][0]["blocks"][0]
        assert block["directive"] == directive

    def test_operator_dict_keys_and_user_ids_are_quoted(self):
        """The bare-key half of ISSUE-443, in this file rather than the
        template.

        `options` and a source's `config` are operator YAML, and the user id
        becomes part of the table path. A bare TOML key admits only
        `[A-Za-z0-9_-]`, so a space stops the file parsing and a dot silently
        re-homes the value — `users.al.ice.briefings` files everything under a
        user nobody has, and `{ max.items = 3 }` becomes a nested table.
        """
        users = {"al.ice": {"briefings": [{
            "name": "q", "cron": "* * * * *",
            "blocks": [{
                "title": "t",
                "options": {"max items": 3, "max.depth": 2},
                "sources": [{"kind": "notes", "config": {"a b": "x"}}],
            }],
        }]}}
        parsed = tomllib.loads(render(users))
        assert list(parsed["users"]) == ["al.ice"]
        block = parsed["users"]["al.ice"]["briefings"][0]["blocks"][0]
        assert block["options"] == {"max items": 3, "max.depth": 2}
        assert block["sources"][0]["config"] == {"a b": "x"}


class TestDefaultBriefingsTomlFilter:
    _DEFAULTS = [
        {
            "name": "Daily",
            "cron": "0 7 * * *",
            "output": "talk",
            "blocks": [
                {
                    "title": "World News",
                    "render_mode": "synthesis",
                    "sources": [{"kind": "browse", "config": {"preset": "ap"}}],
                },
                {
                    "title": "Markets",
                    "render_mode": "structured",
                    "sources": [{"kind": "markets", "config": {}}],
                },
            ],
        },
    ]

    def test_empty_when_no_defaults(self):
        assert render_defaults([]) == ""
        assert render_defaults("nope") == ""

    def test_renders_parseable_default_briefings_section(self):
        out = render_defaults(self._DEFAULTS)
        parsed = tomllib.loads(out)
        assert "default_briefings" in parsed
        entries = parsed["default_briefings"]
        assert len(entries) == 1
        assert entries[0]["name"] == "Daily"
        assert entries[0]["cron"] == "0 7 * * *"
        assert entries[0]["output"] == "talk"
        assert [b["title"] for b in entries[0]["blocks"]] == ["World News", "Markets"]

    def test_round_trips_into_config_default_briefings(self, tmp_path):
        from istota.config import load_config

        out = render_defaults(self._DEFAULTS)
        p = tmp_path / "config.toml"
        p.write_text(out)
        cfg = load_config(p)
        assert len(cfg.default_briefings) == 1
        d0 = cfg.default_briefings[0]
        assert d0.name == "Daily"
        assert d0.output == "talk"
        assert [b["title"] for b in d0.blocks] == ["World News", "Markets"]


def test_config_example_blocks_round_trip():
    """The worked example in config.example.toml parses and seeds cleanly."""
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.toml"
    parsed = tomllib.loads(example.read_text())
    alice = parsed["users"]["alice"]
    world = [b for b in alice["briefings"] if b["name"] == "world"]
    assert world, "expected a config-authored 'world' briefing with blocks"
    uc = _parse_user_data(alice, "alice")
    world_bc = [b for b in uc.briefings if b.name == "world"][0]
    specs = normalize_block_specs(world_bc.blocks, briefing_name="world")
    assert [s["title"] for s in specs] == ["World News", "Markets"]


render_shared = _FILTER_MOD.istota_briefing_shared_blocks_toml


class TestSharedBlocksToml:
    _SHARED = [
        {
            "name": "world-headlines",
            "cron": "0 6 * * *",
            "title": "🌍 World headlines",
            "directive": "Synthesize the frontpages.",
            "render_mode": "synthesis",
            "enabled": True,
            "sources": [
                {"kind": "browse", "config": {"preset": "ap"}},
                {"kind": "browse", "config": {"preset": "reuters"}},
            ],
        },
    ]

    def test_empty_renders_nothing(self):
        assert render_shared([]) == ""
        assert render_shared(None) == ""

    def test_skips_entry_missing_cron(self):
        assert render_shared([{"name": "x"}]) == ""

    def test_round_trips_into_config(self, tmp_path):
        from istota.config import load_config

        out = render_shared(self._SHARED)
        p = tmp_path / "config.toml"
        p.write_text(out)
        cfg = load_config(p)
        assert len(cfg.briefing_shared_blocks) == 1
        b = cfg.briefing_shared_blocks[0]
        assert b.name == "world-headlines"
        assert b.cron == "0 6 * * *"
        assert [s["kind"] for s in b.sources] == ["browse", "browse"]
        assert b.sources[0]["config"] == {"preset": "ap"}

    def test_trusted_renders_and_round_trips(self, tmp_path):
        from istota.config import load_config

        blocks = [{
            "name": "markets-summary", "cron": "30 6 * * *", "title": "📈 Markets",
            "render_mode": "structured", "enabled": True, "trusted": True,
            "sources": [{"kind": "markets", "config": {}}],
        }]
        out = render_shared(blocks)
        assert "trusted = true" in out
        p = tmp_path / "config.toml"
        p.write_text(out)
        cfg = load_config(p)
        assert cfg.briefing_shared_blocks[0].trusted is True


def test_ansible_default_shared_blocks_match_code_defaults(tmp_path):
    """The Ansible istota_briefing_shared_blocks default renders TOML that
    round-trips to the same block names as the code DEFAULT_SHARED_BLOCKS."""
    import yaml

    from istota.config import DEFAULT_SHARED_BLOCKS, load_config

    defaults_path = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "ansible" / "defaults" / "main.yml"
    )
    data = yaml.safe_load(defaults_path.read_text())
    ansible_blocks = data["istota_briefing_shared_blocks"]

    out = render_shared(ansible_blocks)
    p = tmp_path / "config.toml"
    p.write_text(out)
    cfg = load_config(p)
    assert {b.name for b in cfg.briefing_shared_blocks} == {
        b["name"] for b in DEFAULT_SHARED_BLOCKS
    }
