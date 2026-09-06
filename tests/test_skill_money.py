"""Tests for the money skill (in-process facade over the vendored money package)."""

import os
from unittest.mock import MagicMock, patch

import pytest


def _argparse_refusal(argv: list[str]) -> str:
    """The skill parser's own stderr for an argv it refuses.

    Asserting only on the exit code would be vacuous: argparse answers 2 for a
    subcommand that does not exist at all, so a test written that way passes
    before the verb is built. The caller matches on the message instead.
    """
    import io
    from contextlib import redirect_stderr

    from istota.skills.money import build_parser

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2
    return err.getvalue()


def _empty_skills_dir(tmp_path):
    d = tmp_path / "_empty_skills"
    d.mkdir(exist_ok=True)
    return d


class TestMoneySkillManifest:
    """skill.md loads as a keyword-only skill after the modules refactor."""

    def test_load_skill(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        assert "money" in index
        meta = index["money"]
        assert meta.cli is True
        # Module-shaped skills no longer carry resource_types; selection is
        # keyword-only and execution is gated by is_module_enabled.
        assert meta.resource_types == []
        assert "accounting" in meta.keywords

    def test_not_eager_selected_but_in_menu(self, tmp_path):
        # money is a menu skill now (single-axis model): keyword no longer
        # eager-selects it; it's surfaced in the on-demand catalogue instead.
        from istota.skills._loader import (
            eligible_skill_names, load_skill_index, select_skills,
        )

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="check my balances",
            source_type="talk",
            user_resource_types=set(),
            skill_index=index,
        )
        assert "money" not in selected
        menu = eligible_skill_names(index, exclude=set(selected))
        assert "money" in menu

    def test_not_selected_without_keyword(self, tmp_path):
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="hello there",
            source_type="talk",
            user_resource_types=set(),
            skill_index=index,
        )
        assert "money" not in selected

    def test_env_specs(self, tmp_path):
        """MONEY_USER plus the Monarch cookie pair (the only credential we
        store post-cookie-auth refactor)."""
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["money"]
        env_vars = {spec.var for spec in meta.env_specs}
        assert env_vars == {
            "MONEY_USER",
            "MONARCH_SESSION_ID",
            "MONARCH_CSRFTOKEN",
        }
        sensitive = {s.var for s in meta.env_specs if s.sensitive}
        assert sensitive == {
            "MONARCH_SESSION_ID",
            "MONARCH_CSRFTOKEN",
        }


class TestRunInProcess:
    """The _run helper resolves UserContext in-process and invokes money.cli.cli."""

    def _patch_resolver(self, user_ctx=None):
        """Patch load_config + resolve_for_user to return a UserContext stub."""
        from contextlib import ExitStack
        from unittest.mock import patch

        stack = ExitStack()
        stack.enter_context(patch("istota.config.load_config", return_value=MagicMock()))
        stack.enter_context(patch(
            "istota.money.resolve_for_user",
            return_value=user_ctx or MagicMock(),
        ))
        return stack

    def test_run_returns_parsed_json(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = '{"status": "ok", "data": [1, 2]}\n'

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result == {"status": "ok", "data": [1, 2]}
        invoke_args = MockRunner.return_value.invoke.call_args
        passed_args = invoke_args[0][1]
        # User key flows through as -u
        assert passed_args[:2] == ["-u", "alice"]
        assert passed_args[-1] == "list"
        # Pre-built Context injected via obj=
        assert "obj" in invoke_args.kwargs

    def test_run_errors_when_user_not_set(self):
        from istota.skills.money import _run

        with patch.dict(os.environ, {}, clear=True):
            result = _run(["list"])

        assert result["status"] == "error"
        assert "MONEY_USER" in result["error"]

    def test_main_exits_nonzero_on_error_envelope(self):
        """The scheduler relies on a non-zero exit to detect failure. Without
        this, run-scheduled errors (broken Monarch credentials, missing config,
        etc.) silently report status=completed."""
        from istota.skills.money import main

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                main(["list"])

        assert exc_info.value.code == 1

    def test_run_errors_when_user_not_resolved(self):
        from istota.skills.money import _run
        from istota.money import UserNotFoundError

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             patch("istota.config.load_config", return_value=MagicMock()), \
             patch(
                 "istota.money.resolve_for_user",
                 side_effect=UserNotFoundError("no money for alice"),
             ):
            result = _run(["list"])

        assert result["status"] == "error"
        assert "no money for alice" in result["error"]

    def test_run_returns_error_on_nonzero_exit(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 2
        fake_result.output = "boom\n"

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result["status"] == "error"
        assert "boom" in result["error"]

    def test_run_returns_error_on_exception(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = RuntimeError("kaboom")
        fake_result.exit_code = 1
        fake_result.output = ""

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result["status"] == "error"
        assert "kaboom" in result["error"]

    def test_run_returns_error_on_invalid_json(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = "not json"

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result["status"] == "error"
        assert "invalid JSON" in result["error"]

    def test_run_threads_user_secrets_into_context(self):
        """Without this, scheduler-driven sync-monarch runs with no credentials."""
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = '{"status": "ok"}'

        secrets = {"monarch": {"session_id": "sid-abc", "csrftoken": "csrf-abc"}}

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("istota.money.load_user_secrets", return_value=secrets), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            _run(["sync-monarch"])

        invoke_kwargs = MockRunner.return_value.invoke.call_args.kwargs
        passed_obj = invoke_kwargs["obj"]
        assert passed_obj.secrets == secrets


class TestCommandDispatch:
    """End-to-end: each cmd_X composes args and routes through _run."""

    @pytest.fixture
    def captured(self):
        captured = []

        def fake_run(args):
            captured.append(args)
            return {"status": "ok"}

        with patch("istota.skills.money._run", side_effect=fake_run):
            yield captured

    def test_list(self, captured):
        from istota.skills.money import main

        main(["list"])
        assert captured[-1] == ["list"]

    def test_balances_with_filters(self, captured):
        from istota.skills.money import main

        main(["balances", "--ledger", "personal", "--account", "Expenses:Food"])
        args = captured[-1]
        assert args[0] == "balances"
        assert "--ledger" in args and "personal" in args
        assert "--account" in args and "Expenses:Food" in args

    def test_invoice_void_with_force(self, captured):
        from istota.skills.money import main

        main(["invoice", "void", "INV-000001", "--force", "--delete-pdf"])
        args = captured[-1]
        assert args[:3] == ["invoice", "void", "INV-000001"]
        assert "--force" in args
        assert "--delete-pdf" in args

    def test_invoice_unpaid(self, captured):
        from istota.skills.money import main

        main(["invoice", "unpaid", "INV-000001"])
        assert captured[-1] == ["invoice", "unpaid", "INV-000001"]

    def test_work_add(self, captured):
        from istota.skills.money import main

        main([
            "work", "add",
            "--date", "2026-02-01",
            "--client", "acme",
            "--service", "dev",
            "--qty", "4",
        ])
        args = captured[-1]
        assert args[:2] == ["work", "add"]
        assert "--date" in args and "2026-02-01" in args
        # --qty is parsed as float by argparse; str(4.0) -> "4.0"
        assert "--qty" in args and "4.0" in args

    def test_unknown_command_exits(self):
        from istota.skills.money import main

        with pytest.raises(SystemExit):
            main(["nonexistent-command"])

    def test_sync_monarch_defaults_to_matching_invoices(self, captured):
        """ISSUE-083: matching is on unless the caller opts out."""
        from istota.skills.money import main

        main(["sync-monarch"])
        assert captured[-1] == ["sync-monarch"]

    def test_sync_monarch_invoice_matching_flags(self, captured):
        from istota.skills.money import main

        main(["sync-monarch", "--no-match-invoices", "--tolerance", "5"])
        args = captured[-1]
        assert args[0] == "sync-monarch"
        assert "--no-match-invoices" in args
        assert "--tolerance" in args and "5.0" in args

    def test_run_scheduled_invoice_matching_flags(self, captured):
        """The cron path needs its own off switch; --skip-monarch is not one."""
        from istota.skills.money import main

        main(["run-scheduled", "--no-match-invoices", "--tolerance", "5"])
        args = captured[-1]
        assert args[0] == "run-scheduled"
        assert "--skip-monarch" not in args
        assert "--no-match-invoices" in args
        assert "--tolerance" in args and "5.0" in args

    def test_portfolio_import(self, captured):
        from istota.skills.money import main

        main(["portfolio", "import", "/tmp/pos.csv", "--source",
              "fidelity-positions-csv", "--dry-run"])
        args = captured[-1]
        assert args[:3] == ["portfolio", "import", "/tmp/pos.csv"]
        assert "--source" in args and "fidelity-positions-csv" in args
        assert "--dry-run" in args

    def test_portfolio_import_replace(self, captured):
        from istota.skills.money import main

        main(["portfolio", "import", "/tmp/pos.csv", "--replace", "3"])
        args = captured[-1]
        assert "--replace" in args and "3" in args

    def test_portfolio_autoclass(self, captured):
        from istota.skills.money import main

        main(["portfolio", "autoclass"])
        assert captured[-1] == ["portfolio", "autoclass"]

    def test_portfolio_summary_with_group(self, captured):
        from istota.skills.money import main

        main(["portfolio", "summary", "--snapshot", "5", "--group", "Carol"])
        args = captured[-1]
        assert args[:2] == ["portfolio", "summary"]
        assert "--snapshot" in args and "5" in args
        assert "--group" in args and "Carol" in args

    def test_portfolio_history_grouped(self, captured):
        from istota.skills.money import main

        main(["portfolio", "history", "--group-by", "asset_class"])
        args = captured[-1]
        assert "--group-by" in args and "asset_class" in args

    def test_portfolio_diff_and_symbol(self, captured):
        from istota.skills.money import main

        main(["portfolio", "diff", "1", "2"])
        assert captured[-1] == ["portfolio", "diff", "1", "2"]
        main(["portfolio", "symbol", "VTI"])
        assert captured[-1] == ["portfolio", "symbol", "VTI"]

    def test_portfolio_delete_snapshot_confirmed(self, captured):
        from istota.skills.money import main

        main(["portfolio", "delete-snapshot", "7", "--confirmed"])
        args = captured[-1]
        assert args[:3] == ["portfolio", "delete-snapshot", "7"]
        assert "--confirmed" in args

    def test_portfolio_accounts_mutations(self, captured):
        from istota.skills.money import main

        main(["portfolio", "accounts", "--set-group", "3", "Carol"])
        args = captured[-1]
        assert args[:2] == ["portfolio", "accounts"]
        assert "--set-group" in args and "3" in args and "Carol" in args
        main(["portfolio", "accounts", "--exclude", "4"])
        args = captured[-1]
        assert "--exclude" in args and "4" in args

    def test_portfolio_classify(self, captured):
        from istota.skills.money import main

        main(["portfolio", "classify", "GOOG", "--asset-class", "Stocks",
              "--sub-class", "Technology", "--geography", "US"])
        args = captured[-1]
        assert args[:3] == ["portfolio", "classify", "GOOG"]
        assert "--asset-class" in args and "Stocks" in args
        main(["portfolio", "unclassify", "GOOG"])
        assert captured[-1] == ["portfolio", "unclassify", "GOOG"]

    def test_portfolio_classifications(self, captured):
        from istota.skills.money import main

        main(["portfolio", "classifications"])
        assert captured[-1] == ["portfolio", "classifications"]


class TestExecutorIntegration:
    """The in-process skill needs neither an API-key proxy var nor a network host."""

    def _idx(self):
        from istota.skills._loader import load_skill_index
        from pathlib import Path
        return load_skill_index(Path("config/skills"), bundled_dir=None)

    def test_no_money_api_key_in_proxy_vars(self):
        from istota.executor import derive_credential_set

        creds = derive_credential_set(self._idx())
        # Legacy out-of-process names that used to live here.
        assert "MONEYMAN_API_KEY" not in creds
        assert "MONEY_API_KEY" not in creds

    def test_no_money_api_key_in_credential_skill_map(self):
        from istota.executor import derive_skill_credential_map

        idx = self._idx()
        result = derive_skill_credential_map(list(idx.keys()), idx)
        for creds in result.values():
            assert "MONEYMAN_API_KEY" not in creds
            assert "MONEY_API_KEY" not in creds




class TestMoneyLoaderEnvFirst:
    """Phase 1.2 — money loader reads env vars before consulting secrets_store.

    Pinned because Phase 1.4 strips ISTOTA_SECRET_KEY from subprocess env;
    once that lands the secrets_store fallback returns None silently and
    cron module jobs would lose access to MONARCH_* without env-first
    resolution.
    """

    _ALL_ENV_VARS = (
        "MONARCH_SESSION_ID",
        "MONARCH_CSRFTOKEN",
    )

    def test_env_takes_precedence_over_store(self, tmp_path, monkeypatch):
        from istota.config import Config, UserConfig
        from istota.money._loader import load_user_secrets

        cfg = Config(
            db_path=tmp_path / "istota.db",
            users={"alice": UserConfig()},
        )
        monkeypatch.setenv("MONARCH_SESSION_ID", "env-sid")
        monkeypatch.setenv("MONARCH_CSRFTOKEN", "env-csrf")
        called = []
        monkeypatch.setattr(
            "istota.secrets_store.get_secret",
            lambda *a, **kw: called.append(a) or "from-store",
        )

        result = load_user_secrets("alice", cfg)
        assert result == {"monarch": {
            "session_id": "env-sid",
            "csrftoken": "env-csrf",
        }}
        assert called == []

    def test_store_fallback_when_env_unset(self, tmp_path, monkeypatch):
        """Daemon-context: env unset, store wins."""
        from istota.config import Config, UserConfig
        from istota.money._loader import load_user_secrets

        cfg = Config(
            db_path=tmp_path / "istota.db",
            users={"alice": UserConfig()},
        )
        for v in self._ALL_ENV_VARS:
            monkeypatch.delenv(v, raising=False)

        def fake_get(db, u, s, k):
            return {
                "session_id": "store-sid",
                "csrftoken": "store-csrf",
            }.get(k)

        monkeypatch.setattr("istota.secrets_store.get_secret", fake_get)
        result = load_user_secrets("alice", cfg)
        assert result == {"monarch": {
            "session_id": "store-sid",
            "csrftoken": "store-csrf",
        }}

    def test_partial_env_partial_store(self, tmp_path, monkeypatch):
        """Mixed: env supplies one cookie, store fills the other."""
        from istota.config import Config, UserConfig
        from istota.money._loader import load_user_secrets

        cfg = Config(
            db_path=tmp_path / "istota.db",
            users={"alice": UserConfig()},
        )
        monkeypatch.setenv("MONARCH_SESSION_ID", "env-sid")
        monkeypatch.delenv("MONARCH_CSRFTOKEN", raising=False)

        def fake_get(db, u, s, k):
            return {"csrftoken": "store-csrf"}.get(k)

        monkeypatch.setattr("istota.secrets_store.get_secret", fake_get)
        result = load_user_secrets("alice", cfg)
        assert result == {"monarch": {
            "session_id": "env-sid",
            "csrftoken": "store-csrf",
        }}


class TestMonarchCategoryMap:
    """The host-side route for reading and setting a Monarch category mapping.

    Config rather than ledger work, so it goes to config_store directly rather
    than through the Click tree. The scope is always the task's own user:
    MONEY_USER is set by the framework from the task's user_id, and there is no
    flag that can name a different one.
    """

    @pytest.fixture
    def ctx(self, tmp_path):
        from istota.money import config_store
        from istota.money.cli import UserContext

        db_path = tmp_path / "money" / "data" / "money.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        config_store.init_db(db_path)
        return UserContext(
            data_dir=tmp_path / "money", ledgers=[], db_path=db_path,
        )

    @pytest.fixture
    def run(self, ctx, capsys):
        import json as _json

        from istota.skills.money import main

        def _run(argv):
            with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
                 patch("istota.config.load_config", return_value=MagicMock()), \
                 patch("istota.money.resolve_for_user", return_value=ctx):
                try:
                    main(argv)
                except SystemExit as exc:
                    # `_output` exits 1 after printing the error envelope.
                    # Anything else is argparse refusing the argv, which the
                    # scope and user-flag cases assert on directly.
                    if exc.code != 1:
                        raise
                    return _json.loads(capsys.readouterr().out), 1
            return _json.loads(capsys.readouterr().out), 0

        return _run

    def test_set_then_list(self, run):
        body, code = run([
            "monarch-category-map", "set", "--global",
            "--category", "Internet Services (Reimbursed)",
            "--account", "Expenses:Internet-Services",
        ])
        assert code == 0
        assert body["status"] == "ok"
        assert body["state"] == "created"

        body, code = run(["monarch-category-map", "list", "--global"])
        assert code == 0
        assert body["mapping"] == {
            "Internet Services (Reimbursed)": "Expenses:Internet-Services",
        }

    def test_set_refuses_an_account_beancount_cannot_parse(self, run):
        body, code = run([
            "monarch-category-map", "set", "--global",
            "--category", "Internet Services (Reimbursed)",
            "--account", "Expenses:Uncategorized:InternetServices(Reimbursed)",
        ])
        assert code == 1
        assert body["status"] == "error"
        assert "category-map" in body["error"]

        body, _ = run(["monarch-category-map", "list", "--global"])
        assert body["mapping"] == {}

    def test_set_on_a_profile_that_does_not_exist_is_an_error(self, run):
        body, code = run([
            "monarch-category-map", "set", "--profile", "nope",
            "--category", "Fees", "--account", "Expenses:Fees",
        ])
        assert code == 1
        assert body["status"] == "error"

    def test_a_scope_is_required(self, run):
        with pytest.raises(SystemExit) as exc:
            run(["monarch-category-map", "list"])
        assert exc.value.code == 2

    def test_there_is_no_flag_for_another_user(self, run):
        with pytest.raises(SystemExit) as exc:
            run([
                "monarch-category-map", "list", "--global", "--user", "bob",
            ])
        assert exc.value.code == 2

    def test_the_scope_comes_from_the_environment(self, ctx, capsys):
        from istota.skills.money import main

        with patch.dict(os.environ, {}, clear=True), \
             patch("istota.config.load_config", return_value=MagicMock()), \
             patch("istota.money.resolve_for_user", return_value=ctx), \
             pytest.raises(SystemExit):
            main(["monarch-category-map", "list", "--global"])
        assert "MONEY_USER not set" in capsys.readouterr().out


class TestTransactionRules:
    """The model-facing front end over `transaction_rules`.

    Three verbs where the operator CLI has five: `list` and `test` read, `set`
    writes. There is no delete, matching `monarch-category-map`, where
    removing an entry is an operator command — a rule the model can delete is
    a `skip` the model can quietly stop applying.

    The scope is always the task's own user. `MONEY_USER` is set by the
    framework from the task's user_id and no verb takes a user as an argument.
    """

    @pytest.fixture
    def ctx(self, tmp_path):
        from istota.money import config_store
        from istota.money.cli import UserContext

        db_path = tmp_path / "money" / "data" / "money.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        config_store.init_db(db_path)
        return UserContext(
            data_dir=tmp_path / "money", ledgers=[], db_path=db_path,
        )

    @pytest.fixture
    def run(self, ctx, capsys):
        import json as _json

        from istota.skills.money import main

        def _run(argv):
            with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
                 patch("istota.config.load_config", return_value=MagicMock()), \
                 patch("istota.money.resolve_for_user", return_value=ctx):
                try:
                    main(argv)
                except SystemExit as exc:
                    if exc.code != 1:
                        raise
                    return _json.loads(capsys.readouterr().out), 1
            return _json.loads(capsys.readouterr().out), 0

        return _run

    def _user_rules(self, run):
        body, _ = run(["transaction-rules", "list"])
        return [r for r in body["rules"] if r["origin"] == "user"]

    SET = [
        "transaction-rules", "set", "--ledger", "personal",
        "--source", "monarch-api", "--field", "category",
        "--match-value", "Software", "--action", "posting_account",
        "--target", "Expenses:Biz:Software",
    ]

    def test_set_then_list(self, run):
        body, code = run(self.SET)
        assert code == 0
        assert body["status"] == "ok"
        assert body["state"] == "created"
        rule_id = body["rule"]["id"]

        body, code = run([
            "transaction-rules", "list",
            "--ledger", "personal", "--source", "monarch-api",
        ])
        assert code == 0
        assert [r["id"] for r in body["rules"]] == [rule_id]
        assert body["rules"][0]["target"] == "Expenses:Biz:Software"

    def test_a_rule_the_model_writes_is_a_user_rule(self, run):
        """`origin` is not an argument. The store reserves `seed` for the
        shipped set and a caller claiming it wedges every later map write in
        that scope, so the surface offers no way to say anything else."""
        body, _ = run(self.SET)
        assert body["rule"]["origin"] == "user"

    def test_the_scope_must_be_sent_rather_than_defaulted_into(self, run):
        """Both columns default to `''`, which the engine reads as "any", so
        an omitted ledger is a rule the model wrote for every ledger and every
        source at once.

        Not argparse's refusal, and it cannot be: `--id` makes the scope
        conditional, since a `set` naming a stored rule leaves the scope
        somebody already chose alone. So it is the verb's own check, and it
        answers in the envelope every other skill error uses.
        """
        body, code = run([a for a in self.SET if a not in ("--ledger", "personal")])
        assert code == 1
        assert "--ledger" in body["error"]
        assert self._user_rules(run) == []

    def test_a_set_naming_a_stored_rule_needs_no_scope(self, run):
        created, _ = run(self.SET)
        body, code = run([
            "transaction-rules", "set", "--id", str(created["rule"]["id"]),
            "--priority", "50",
        ])
        assert code == 0
        assert body["rule"]["ledger"] == "personal"
        assert body["rule"]["priority"] == 50

    def test_set_with_an_id_updates_the_named_rule(self, run):
        body, _ = run(self.SET)
        rule_id = body["rule"]["id"]
        body, code = run([
            "transaction-rules", "set", "--id", str(rule_id),
            "--target", "Expenses:Biz:Tools",
        ])
        assert code == 0
        assert body["state"] == "updated"
        assert body["rule"]["target"] == "Expenses:Biz:Tools"
        assert body["rule"]["match_value"] == "Software"

    def test_set_with_an_id_that_is_not_there(self, run):
        body, code = run([
            "transaction-rules", "set", "--id", "4242",
            "--target", "Expenses:X",
        ])
        assert code == 1
        assert body["status"] == "error"
        assert "4242" in body["error"]

    def test_a_duplicate_names_the_id_to_update_and_not_the_value(self, run):
        """The whole loop the model has to be able to walk: a second `set` for
        a scope, match and action already written is refused, and the refusal
        carries the id the model then passes to `--id`."""
        first, _ = run(self.SET)
        body, code = run(self.SET[:-1] + ["Expenses:Somewhere:Else"])
        assert code == 1
        assert f"id {first['rule']['id']}" in body["error"]
        assert "Software" not in body["error"]
        assert "Expenses:Somewhere:Else" not in body["error"]

        body, code = run([
            "transaction-rules", "set", "--id", str(first["rule"]["id"]),
            "--target", "Expenses:Somewhere:Else",
        ])
        assert code == 0

    def test_a_bad_account_is_refused_without_echoing_it(self, run):
        """A skill error reaches the model's context and a Talk room, so a
        validation message names the field and the constraint rather than the
        user's own financial data."""
        body, code = run(self.SET[:-1] + ["expenses:nope"])
        assert code == 1
        assert "target" in body["error"]
        assert "expenses:nope" not in body["error"]

    def test_an_over_long_match_value_is_refused_without_echoing_it(self, run):
        value = "x" * 400
        argv = list(self.SET)
        argv[argv.index("Software")] = value
        body, code = run(argv)
        assert code == 1
        assert "match_value" in body["error"]
        assert value not in body["error"]

    def test_test_resolves_a_made_up_transaction(self, run):
        created, _ = run(self.SET)
        body, code = run([
            "transaction-rules", "test",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "software",
        ])
        assert code == 0
        assert body["resolution"]["posting_account"] == "Expenses:Biz:Software"
        assert [h["rule_id"] for h in body["resolution"]["hits"]] == [
            created["rule"]["id"],
        ]

    def test_test_reports_a_skip(self, run):
        run([
            "transaction-rules", "set", "--ledger", "personal", "--source", "",
            "--field", "tag", "--match-value", "Personal", "--action", "skip",
            "--priority", "50",
        ])
        body, code = run([
            "transaction-rules", "test",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "Software", "--tag", "Personal",
        ])
        assert code == 0
        assert body["resolution"]["skip"] is True

    def test_test_refuses_on_a_deployment_the_migration_has_not_reached(
        self, run, monkeypatch,
    ):
        """`load_rules_for_run` answering None means an import still resolves
        from the legacy maps, so a preview off the table would tell the model
        about behaviour this deployment does not have."""
        from istota.money import config_store

        monkeypatch.setattr(config_store, "_rules_migrated", lambda conn: False)
        body, code = run([
            "transaction-rules", "test",
            "--ledger", "personal", "--source", "monarch-api",
            "--category", "Software",
        ])
        assert code == 1
        assert "migration" in body["error"]

    def test_there_is_no_flag_for_another_user(self, run):
        assert "--user" in _argparse_refusal([
            "transaction-rules", "list", "--ledger", "personal",
            "--source", "monarch-api", "--user", "bob",
        ])

    def test_there_is_no_verb_that_deletes(self, run):
        """Asserting `"remove" in stderr` was not enough: argparse prints the
        verb in its own usage line, so any real delete verb — one with a
        required flag, as it would have — was refused for the missing flag and
        passed the test. The verb set is the property, so assert it."""
        from istota.skills.money import build_parser

        rules = build_parser()._subparsers._group_actions[0].choices[
            "transaction-rules"
        ]
        actions = rules._subparsers._group_actions[0].choices
        assert set(actions) == {"list", "set", "test"}
        assert "invalid choice: 'remove'" in _argparse_refusal(
            ["transaction-rules", "remove", "--id", "1"],
        )

    def test_set_can_switch_a_rule_off_and_back_on(self, run):
        """`include_disabled=not args.enabled_only` is its own expression on
        this surface, and an inverted flag survives every other test here."""
        created, _ = run(self.SET)
        rule_id = created["rule"]["id"]
        run(["transaction-rules", "set", "--ledger", "personal",
             "--source", "monarch-api", "--field", "category",
             "--match-value", "Hosting", "--action", "posting_account",
             "--target", "Expenses:Biz:Hosting"])

        body, code = run(["transaction-rules", "set", "--id", str(rule_id),
                          "--disable"])
        assert code == 0
        assert body["rule"]["enabled"] is False

        body, _ = run(["transaction-rules", "list", "--ledger", "personal",
                       "--source", "monarch-api", "--enabled-only"])
        assert [r["match_value"] for r in body["rules"]] == ["Hosting"]

        body, _ = run(["transaction-rules", "list", "--ledger", "personal",
                       "--source", "monarch-api"])
        assert len(body["rules"]) == 2

        body, code = run(["transaction-rules", "set", "--id", str(rule_id),
                          "--enable"])
        assert body["rule"]["enabled"] is True

    def test_set_keeps_a_falsy_value_the_argv_named(self, run):
        """`''` is "any scope" and `0` is a legal priority, so a builder
        testing truthiness would drop both and leave the stored value
        standing. Only the merge path can catch that: on a create the store's
        own defaults are the same two values."""
        created, _ = run(self.SET)
        body, code = run([
            "transaction-rules", "set", "--id", str(created["rule"]["id"]),
            "--ledger", "", "--priority", "0",
        ])
        assert code == 0
        assert body["rule"]["ledger"] == ""
        assert body["rule"]["priority"] == 0
        assert body["rule"]["source"] == "monarch-api"

    def test_a_lost_duplicate_race_is_not_a_traceback(self, run):
        """The store's check-then-insert is not atomic across connections, so
        the unique index can still refuse. The handler answers without an id,
        matching what the HTTP create answers when it loses the same race."""
        import sqlite3

        from istota.money import config_store

        with patch.object(
            config_store, "create_transaction_rule",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed"),
        ):
            body, code = run(self.SET)
        assert code == 1
        assert "already exists" in body["error"]
        assert "id " not in body["error"]

    def test_a_preview_refuses_more_tags_than_the_http_preview_does(self, run):
        """The model writes this argv and `--tag` is repeatable. Refused
        rather than cut, matching the route: dropping a tag silently changes
        which rules fire."""
        from istota.money.core import rules as rule_engine

        args = []
        for i in range(rule_engine.MAX_PREVIEW_TAGS + 1):
            args += ["--tag", f"t{i}"]
        body, code = run([
            "transaction-rules", "test", "--ledger", "personal",
            "--source", "monarch-api", *args,
        ])
        assert code == 1
        assert str(rule_engine.MAX_PREVIEW_TAGS) in body["error"]

    def test_the_scope_comes_from_the_environment(self, ctx, capsys):
        from istota.skills.money import main

        with patch.dict(os.environ, {}, clear=True), \
             patch("istota.config.load_config", return_value=MagicMock()), \
             patch("istota.money.resolve_for_user", return_value=ctx), \
             pytest.raises(SystemExit):
            main([
                "transaction-rules", "list", "--ledger", "personal",
                "--source", "monarch-api",
            ])
        assert "MONEY_USER not set" in capsys.readouterr().out
