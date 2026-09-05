"""Tests for Config.is_shared_kv_writer — the fail-closed shared-write gate.

Pins the deliberate asymmetry vs is_admin so a future refactor can't collapse
the two.
"""

from istota.config import Config


class TestSharedKvWriter:
    def test_empty_allowlist_authorizes_nobody(self):
        config = Config(admin_users=set())
        # The fail-closed assertion, contrasted with is_admin's empty-means-all.
        assert config.is_shared_kv_writer("alice") is False
        assert config.is_admin("alice") is True

    def test_member_authorized(self):
        config = Config(admin_users={"alice", "bob"})
        assert config.is_shared_kv_writer("alice") is True
        assert config.is_shared_kv_writer("bob") is True

    def test_non_member_denied(self):
        config = Config(admin_users={"alice"})
        assert config.is_shared_kv_writer("mallory") is False

    def test_asymmetry_with_is_admin_on_populated_allowlist(self):
        # On a populated allowlist both agree for members and non-members.
        config = Config(admin_users={"alice"})
        assert config.is_shared_kv_writer("alice") == config.is_admin("alice")
        assert config.is_shared_kv_writer("mallory") == config.is_admin("mallory")


class TestStandaloneExemption:
    """The single-user shape's exemption.

    A standalone install has no admins file to be named in, so the fail-closed
    rule above locked its one user out of shared briefing blocks. The exemption
    is the backstop for an install made before the wizard started writing an
    admins file, and it is deliberately narrower than
    ``web_app._user_is_web_admin``'s, which keys on the ``[web] auth`` axis
    alone.
    """

    def _standalone(self, **kw):
        from istota.config import WebConfig

        return Config(web=WebConfig(auth="none"), **kw)

    def test_standalone_local_user_authorized(self):
        config = self._standalone()
        assert config.is_standalone is True
        assert config.is_shared_kv_writer(config.local_user_id) is True

    def test_standalone_other_user_denied(self):
        config = self._standalone()
        assert config.is_shared_kv_writer("mallory") is False

    def test_nextcloud_backed_with_auth_off_is_not_standalone(self):
        """The narrowness the docstring claims. A deployment with Nextcloud
        storage and auth switched off is not the single-user shape and must not
        silently gain a shared-content writer."""
        from istota.config import NextcloudConfig, WebConfig

        config = Config(
            web=WebConfig(auth="none"),
            nextcloud=NextcloudConfig(url="https://nextcloud.example.com"),
        )
        assert config.is_standalone is False
        assert config.is_shared_kv_writer(config.local_user_id) is False

    def test_a_populated_admins_file_still_decides_on_standalone(self):
        config = self._standalone(admin_users={"alice"})
        # local_user_id is "alice" here (sole admin), so a third name is what
        # shows the allowlist is still doing the deciding.
        assert config.is_shared_kv_writer("alice") is True
        assert config.is_shared_kv_writer("mallory") is False

    def test_an_admins_file_excluding_the_local_user_refuses(self, make_user_config):
        """The exemption is a backstop, not an override. Once a real admins
        file exists — which the wizard now writes on every fresh install — it
        decides, so a standalone operator who edits themselves out of it is
        refused rather than silently overridden."""
        from istota.config import WebConfig

        config = Config(
            web=WebConfig(auth="none"),
            users={"alice": make_user_config()},
            admin_users={"bob"},
        )
        assert config.is_standalone is True
        assert config.local_user_id == "alice"
        assert config.is_shared_kv_writer("alice") is False

    def test_a_second_local_user_disables_the_exemption(self, make_user_config):
        """`local_user_id` falls back to `sorted(users)[0]` when several are
        configured, and says in its own docstring that it is only meaningful
        where there is exactly one. Without the count clause the exemption
        would hand shared-write authority to whichever id sorts first."""
        from istota.config import WebConfig

        config = Config(
            web=WebConfig(auth="none"),
            users={"alice": make_user_config(), "bob": make_user_config()},
        )
        assert config.is_standalone is True
        assert config.local_user_id == "alice"
        assert config.is_shared_kv_writer("alice") is False
        assert config.is_shared_kv_writer("bob") is False
