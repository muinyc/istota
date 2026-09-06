"""Tests for istota.skill_host_paths — the shared host-path allowlist.

A skill CLI runs host-side (the proxy spawns it outside the sandbox), so any
verb taking a host path is an arbitrary-file read or write unless it is scoped.
The module holds two allowlists. ``resolve_host_path`` scopes a path in the
caller's own workspace against the mount roots (devbox ``cp-in``/``cp-out``, kv
``set --value-file``, email ``--attach``); ``resolve_under_repos`` scopes a
worktree against ``DEVELOPER_REPOS_DIR`` for the ``code_review`` CLI. One module
so the roots and the error convention cannot drift apart.

The mount roots mirror what ``build_bwrap_cmd`` binds *for this user*.
``NEXTCLOUD_MOUNT_PATH`` is the shared mount root for everyone, so taking it
whole would hand one user another's workspace.

Several tests here are written specifically to kill a plausible wrong
implementation rather than to describe the right one — an argument-inspecting
check that never resolves, a lexical ``startswith`` containment, a root echoed
back instead of resolved. Where a test looks redundant, that is usually why.
"""

import json
from pathlib import Path

import pytest

from istota.skill_host_paths import (
    allowed_host_roots,
    developer_repos_root,
    resolve_host_path,
    resolve_under_repos,
    user_workspace_root,
    validate_host_path,
    write_resolved,
)


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """A mount laid out like the real one, with alice as the caller."""
    root = tmp_path / "mount"
    (root / "Users" / "alice").mkdir(parents=True)
    (root / "Users" / "bob").mkdir(parents=True)
    (root / "Channels" / "tok1").mkdir(parents=True)
    (root / "Channels" / "tok2").mkdir(parents=True)
    (root / "Talk").mkdir(parents=True)
    deferred = tmp_path / "deferred"
    deferred.mkdir()
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    return root


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """The developer repos tree, and the variable as `execute_task` builds it.

    One fixture for both repos classes, because the shape of the environment is
    the thing under test in each of them and two copies would let one drift.
    `DEVELOPER_REPOS_DIR` is the *caller's own subtree*, not the configured
    root: `setup_env` derives it and `build_bwrap_cmd` binds the same path. Bob
    has a tree next door, which is what the containment tests aim at.
    """
    root = tmp_path / "repos" / "alice"
    (root / "ns" / "project--branch").mkdir(parents=True)
    (tmp_path / "repos" / "bob" / "ns" / "victim--branch").mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    return root


class TestAllowedHostRoots:
    def test_scopes_the_mount_to_the_calling_user(self, mount, monkeypatch):
        roots = allowed_host_roots()
        assert mount / "Users" / "alice" in roots
        assert mount / "Users" / "bob" not in roots
        assert mount not in roots

    def test_includes_the_tasks_own_channel_only(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        roots = allowed_host_roots()
        assert mount / "Channels" / "tok1" in roots
        assert mount / "Channels" / "tok2" not in roots

    def test_talk_is_readable_but_not_writable(self, mount):
        assert mount / "Talk" in allowed_host_roots(writable=False)
        assert mount / "Talk" not in allowed_host_roots(writable=True)

    def test_traversal_token_is_not_turned_into_a_path(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "../..")
        assert all("Channels" not in str(r) for r in allowed_host_roots())

    def test_mount_contributes_nothing_without_a_user_id(self, mount, monkeypatch):
        """Fail closed: without an identity there is no per-user subtree to
        scope to, so the mount contributes no root at all."""
        monkeypatch.delenv("ISTOTA_USER_ID")
        roots = allowed_host_roots()
        assert all(not r.is_relative_to(mount) for r in roots)

    def test_blank_and_unset_are_skipped(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", "   ")
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        assert allowed_host_roots() == []


class TestResolveHostPath:
    def test_accepts_the_users_own_workspace(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_refuses_another_users_workspace(self, mount):
        """The core cross-tenant case: the mount is shared, the bind is not."""
        p = mount / "Users" / "bob" / "private.json"
        p.write_text('{"bobs": "notes"}')
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert resolved is None
        assert "outside allowed roots" in err

    def test_refuses_the_mount_root_itself(self, mount):
        p = mount / "loose.json"
        p.write_text("{}")
        _, err = resolve_host_path(p, writable=False, operation="op")
        assert err is not None

    def test_accepts_the_deferred_dir(self, mount, tmp_path):
        p = tmp_path / "deferred" / "v.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_returns_the_resolved_path_for_the_caller_to_use(self, mount):
        """Handing back the approved path is what lets a caller avoid
        re-walking symlinks on the original."""
        real = mount / "Users" / "alice" / "real.json"
        real.write_text("{}")
        sub = mount / "Users" / "alice" / "sub"
        sub.mkdir()
        resolved, err = resolve_host_path(
            mount / "Users" / "alice" / "sub" / ".." / "real.json",
            writable=False, operation="op",
        )
        assert err is None
        assert resolved == real.resolve()

    def test_refuses_leaf_symlink(self, mount):
        target = mount / "Users" / "bob" / "secret.json"
        target.write_text("{}")
        link = mount / "Users" / "alice" / "link.json"
        link.symlink_to(target)
        _, err = resolve_host_path(link, writable=False, operation="op")
        assert "symlink" in err

    def test_intermediate_symlink_out_of_bounds_is_caught_by_resolution(self, mount):
        """A symlinked *directory* is not the leaf, so the leaf check misses it;
        comparing the fully resolved path is what refuses it."""
        (mount / "Users" / "bob" / "deep").mkdir()
        secret = mount / "Users" / "bob" / "deep" / "s.json"
        secret.write_text("{}")
        hop = mount / "Users" / "alice" / "hop"
        hop.symlink_to(mount / "Users" / "bob" / "deep")
        _, err = resolve_host_path(hop / "s.json", writable=False, operation="op")
        assert err is not None
        assert "outside allowed roots" in err

    def test_missing_source_reported(self, mount):
        _, err = resolve_host_path(
            mount / "Users" / "alice" / "nope.json", writable=False, operation="op",
        )
        assert "not found" in err

    def test_no_roots_refuses_and_names_the_operation(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        _, err = resolve_host_path(tmp_path / "x", writable=False, operation="cp-in/cp-out")
        assert "cp-in/cp-out" in err


class TestResolveHostPathWritable:
    def test_accepts_a_new_file_under_a_root(self, mount):
        dest = mount / "Users" / "alice" / "sub" / "new.txt"
        resolved, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is None
        assert resolved == (mount / "Users" / "alice" / "sub").resolve() / "new.txt"
        assert dest.parent.is_dir()

    def test_does_not_create_directories_outside_the_roots(self, mount, tmp_path):
        """The check must precede the mkdir — creating an out-of-bounds tree as
        the daemon user and only then refusing is still a write."""
        dest = tmp_path / "attacker" / "deep" / "tree" / "x.txt"
        _, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is not None
        assert not (tmp_path / "attacker").exists()

    def test_talk_is_refused_as_a_destination(self, mount):
        _, err = resolve_host_path(
            mount / "Talk" / "x.txt", writable=True, operation="cp-out",
        )
        assert err is not None


class TestValidateHostPathWrapper:
    def test_error_only_wrapper_matches(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        assert validate_host_path(p, must_exist=True, operation="op") is None
        assert validate_host_path(
            mount / "Users" / "bob" / "x", must_exist=True, operation="op",
        ) is not None


class TestDevboxStillDelegates:
    """The devbox skill keeps its private wrapper name, but must not keep a
    second copy of the rule.

    There was a second wrapper here, `_validate_host_path`, which returned the
    error alone. It went when the file verbs moved onto the exec transport: all
    three call sites send the resolved path over the wire, so none of them had a
    use for a variant that threw it away."""

    def test_wrapper_delegates_to_the_shared_validator(self, mount):
        from istota.skills import devbox
        _, err = devbox._resolve_host_path(Path("/etc/passwd"), must_exist=True)
        assert err is not None
        p = mount / "Users" / "alice" / "ok.txt"
        p.write_text("x")
        _, err = devbox._resolve_host_path(p, must_exist=True)
        assert err is None

    def test_resolving_wrapper_returns_the_approved_path(self, mount):
        from istota.skills import devbox
        p = mount / "Users" / "alice" / "ok.txt"
        p.write_text("x")
        resolved, err = devbox._resolve_host_path(p, must_exist=True)
        assert err is None
        assert resolved == p.resolve()

    def test_cross_user_cp_in_is_refused(self, mount):
        from istota.skills import devbox
        p = mount / "Users" / "bob" / "secret.txt"
        p.write_text("x")
        _, err = devbox._resolve_host_path(p, must_exist=True)
        assert err is not None


class TestDeveloperReposRoot:
    """`DEVELOPER_REPOS_DIR` is its own root, separate from the mount ones.

    The review CLI is handed a worktree path chosen by the sandboxed model, and
    it runs host-side with the daemon's filesystem view. Without scoping, "review
    this worktree" is an arbitrary directory read whose contents come back in a
    reviewer prompt.

    The root is *one user's subtree*. `setup_env` derives it and everything
    downstream reads the variable, but this module does not take that on trust:
    it requires `ISTOTA_USER_ID` and requires the root to be the directory that
    user id names. Self-scoping by `ISTOTA_USER_ID` is what the whole module
    does — see its docstring — and here it means a variable that regressed to
    the shared root is refused rather than quietly containing against it.
    """

    def test_returns_the_resolved_root(self, repos):
        assert developer_repos_root() == repos.resolve()

    def test_root_is_resolved_not_echoed(self, tmp_path, monkeypatch):
        """`repos.resolve() == repos` under pytest's tmp_path, so the test above
        cannot tell a resolving implementation from one that echoes the env var.
        A symlinked root can.

        The link is one level *up* from the user's own component, which is the
        shape a symlinked deployment root actually has (`/srv/repos` ->
        `/data/repos`). A link at the user component itself is the planted one,
        refused below.
        """
        physical = tmp_path / "physical"
        (physical / "alice").mkdir(parents=True)
        link = tmp_path / "link-to-physical"
        link.symlink_to(physical)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(link / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() == (physical / "alice").resolve()
        assert developer_repos_root() != link / "alice"

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        assert developer_repos_root() is None

    def test_blank_is_none(self, monkeypatch):
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "   ")
        assert developer_repos_root() is None

    def test_no_user_id_is_none(self, repos, monkeypatch):
        """Refusing is the module's posture for a root it cannot resolve, and
        an unscoped root is one it cannot resolve: without a user id there is
        nothing to check the subtree against."""
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        assert developer_repos_root() is None

    def test_blank_user_id_is_none(self, repos, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "   ")
        assert developer_repos_root() is None

    def test_the_shared_root_is_refused(self, repos, tmp_path, monkeypatch):
        """The variable's pre-split value, refused rather than accepted.

        This is the defence-in-depth half of the per-user layout: if the
        derivation upstream is ever reverted or bypassed, containment here
        stops rather than silently widening to every user's tree.
        """
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(tmp_path / "repos"))
        assert developer_repos_root() is None

    def test_another_users_subtree_is_refused(self, repos, tmp_path, monkeypatch):
        """A root named for somebody else is not this caller's root, however
        well-formed it looks."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(tmp_path / "repos" / "bob"))
        assert developer_repos_root() is None

    def test_a_root_symlinked_to_the_callers_own_subtree_is_refused(
        self, tmp_path, monkeypatch,
    ):
        """The case only the *as-written* half of the check catches.

        Every other refusal here is caught by the resolved name alone, so
        deleting `Path(raw).name != user_id` would leave them all green. Here
        the raw name is `bob` and the resolved one is `alice`, so a check on
        the resolved name says yes to a variable naming somebody else's
        directory. Harmless in itself — the link points at this caller's own
        tree — but it is the half of the guard that answers "is the variable
        the one the layout describes", and without it that question is not
        being asked at all.
        """
        root = tmp_path / "repos"
        (root / "alice").mkdir(parents=True)
        (root / "bob").symlink_to(root / "alice")
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root / "bob"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    @pytest.mark.parametrize("target_name", ["elsewhere", "alice"])
    def test_a_symlinked_user_component_is_refused(
        self, tmp_path, monkeypatch, target_name,
    ):
        """`{repos_dir}` was bound read-write for every admin for as long as it
        was shared, so `{repos_dir}/{user_id}` may already be a symlink a task
        left behind. `executor.get_user_repos_dir` refuses to bind one; this
        refuses to contain against one, so the two seams agree.

        **The target's own name is the parameter, and `alice` is the case that
        matters.** A planted link would be named for its victim, so its target
        can be too — and against a check that compares the *resolved* basename
        to the user id, that one passes. Only the second case distinguishes the
        structural rule from a second name comparison.
        """
        elsewhere = tmp_path / "outside" / target_name
        elsewhere.mkdir(parents=True)
        root = tmp_path / "repos"
        root.mkdir()
        (root / "alice").symlink_to(elsewhere)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None


class TestResolveUnderReposIsPerUser:
    """The second live exposure the per-user layout closes.

    `resolve_under_repos` runs **host-side**, in a skill CLI the proxy spawns
    outside the sandbox with the daemon's filesystem view, and the path it
    validates is one the sandboxed model chose. While `DEVELOPER_REPOS_DIR`
    named the whole of `developer.repos_dir`, `code_review --worktree <another
    admin's checkout>` passed containment and came back as reviewer-prompt
    text. The bwrap-side masks never reached this — they are a property of a
    namespace this process is not in.
    """

    def test_another_users_worktree_is_refused(self, repos, tmp_path):
        """The property, at the value `setup_env` now derives."""
        victim = tmp_path / "repos" / "bob" / "ns" / "victim--branch"
        assert victim.is_dir(), "the victim tree must exist, or this passes on not-found"
        resolved, err = resolve_under_repos(str(victim))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_the_shared_root_gives_no_containment_at_all(self, tmp_path, monkeypatch):
        """The same question asked at the variable's *pre-split* value.

        This is the regression test for the live exposure: against the module
        as it stood before this stage, containment is computed against whatever
        the variable names, so bob's worktree is admitted and returned. It is
        also the control for the test above, which cannot fail while the
        fixture hands it an already-scoped root.
        """
        root = tmp_path / "repos"
        (root / "alice" / "ns" / "wt").mkdir(parents=True)
        (root / "bob" / "ns" / "victim--branch").mkdir(parents=True)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        resolved, err = resolve_under_repos(str(root / "bob" / "ns" / "victim--branch"))
        assert resolved is None
        assert err

    def test_the_callers_own_worktree_still_resolves(self, repos):
        """A refusal that refused everything would pass the two above and
        break every review."""
        resolved, err = resolve_under_repos(str(repos / "ns" / "project--branch"))
        assert err is None
        assert resolved == (repos / "ns" / "project--branch").resolve()


class TestResolveUnderRepos:
    def test_accepts_a_worktree_inside(self, repos):
        wt = repos / "ns" / "project--branch"
        resolved, err = resolve_under_repos(str(wt))
        assert err is None
        assert resolved == wt.resolve()

    def test_returns_the_resolved_path_not_the_argument(self, repos):
        """Callers must operate on what comes back — see the module docstring.

        The argument has to be genuinely non-canonical for this to mean
        anything. `Path("a/./b")` collapses to `a/b` at construction, so a
        `.` component tests nothing; a symlink is a difference pathlib cannot
        normalise away, so only a real `resolve()` produces the target.
        """
        target = repos / "ns" / "project--branch"
        link = repos / "ns" / "via-link"
        link.symlink_to(target)

        resolved, err = resolve_under_repos(str(link))
        assert err is None
        # The argument and the answer are different paths on disk.
        assert Path(str(link)) != target
        assert resolved == target.resolve()

    def test_accepts_a_symlink_that_stays_inside(self, repos):
        """Following links is what catches an escape, so a link that does not
        escape has to be accepted. Pinned because it differs from
        `resolve_host_path`, which refuses a symlinked argument outright."""
        target = repos / "ns" / "project--branch"
        link = repos / "ns" / "inner-link"
        link.symlink_to(target)
        resolved, err = resolve_under_repos(str(link))
        assert err is None
        assert resolved == target.resolve()

    def test_resolves_a_symlinked_root_to_the_physical_path(self, tmp_path, monkeypatch):
        """A root reached through a link must still contain its own worktrees."""
        physical = tmp_path / "physical-repos"
        (physical / "alice" / "ns" / "wt").mkdir(parents=True)
        link_root = tmp_path / "linked-repos"
        link_root.symlink_to(physical)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(link_root / "alice"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        assert developer_repos_root() == (physical / "alice").resolve()
        resolved, err = resolve_under_repos(str(link_root / "alice" / "ns" / "wt"))
        assert err is None
        assert resolved == (physical / "alice" / "ns" / "wt").resolve()

    def test_refuses_a_path_outside(self, repos, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        resolved, err = resolve_under_repos(str(outside))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_refuses_traversal_out(self, repos, tmp_path):
        """The target must EXIST outside the root, or this passes on the
        not-found branch and says nothing about containment.

        Two levels of `..` land in the *shared* root, one step above this
        caller's subtree, which is the traversal that matters now.
        """
        outside = repos.parent / "elsewhere"
        outside.mkdir()
        resolved, err = resolve_under_repos(str(repos / "ns" / ".." / ".." / "elsewhere"))
        assert resolved is None
        assert err and "outside" in err.lower()

    def test_refuses_a_blank_argument(self, repos):
        """`Path("")` is `.`, so without an explicit guard the answer depends
        on the daemon's working directory."""
        for blank in ("", "   "):
            resolved, err = resolve_under_repos(blank)
            assert resolved is None
            assert err and "empty" in err.lower()

    def test_refuses_a_regular_file(self, repos):
        """A worktree is a directory; `git -C <file>` is a confusing failure."""
        f = repos / "ns" / "project--branch" / "README.md"
        f.write_text("x")
        resolved, err = resolve_under_repos(str(f))
        assert resolved is None
        assert err and "directory" in err.lower()

    def test_refuses_a_symlink_pointing_out(self, repos, tmp_path):
        """A symlink planted inside repos_dir is the interesting attack: the
        argument looks compliant and resolution is what catches it."""
        secret = tmp_path / "outside-secrets"
        secret.mkdir()
        link = repos / "ns" / "escape"
        link.symlink_to(secret)
        resolved, err = resolve_under_repos(str(link))
        assert resolved is None
        assert err

    def test_refuses_when_env_unset(self, monkeypatch, tmp_path):
        """Never widen to the whole filesystem because the var is missing."""
        monkeypatch.delenv("DEVELOPER_REPOS_DIR", raising=False)
        resolved, err = resolve_under_repos(str(tmp_path))
        assert resolved is None
        assert err and "DEVELOPER_REPOS_DIR" in err

    def test_refuses_a_missing_path(self, repos):
        resolved, err = resolve_under_repos(str(repos / "ns" / "no-such-worktree"))
        assert resolved is None
        assert err

    def test_repos_root_itself_is_allowed(self, repos):
        """Reviewing at the root is odd but not an escape."""
        resolved, err = resolve_under_repos(str(repos))
        assert err is None
        assert resolved == repos.resolve()

    def test_sibling_prefix_is_not_inside(self, tmp_path, monkeypatch):
        """`.../alice-evil` must not pass because it shares a string prefix
        with `.../alice`. Containment is by path component, not by startswith.

        The sibling is the interesting one now that the root ends in the user
        id: a `startswith` implementation would admit a directory belonging to
        a user whose id merely begins with this one's.
        """
        root = tmp_path / "repos" / "alice"
        root.mkdir(parents=True)
        evil = tmp_path / "repos" / "alice-evil"
        evil.mkdir()
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        resolved, err = resolve_under_repos(str(evil))
        assert resolved is None
        assert err

    def test_does_not_use_the_mount_roots(self, repos, mount):
        """This is a separate allowlist. A path under the user's workspace is
        legitimate for kv/devbox and is not a worktree."""
        resolved, err = resolve_under_repos(str(mount / "Users" / "alice"))
        assert resolved is None
        assert err

    def test_refuses_a_non_path_argument(self, repos):
        """The contract is an error tuple, never a raise. `--worktree` omitted
        gives None, and `Path(None)` is a TypeError that would escape as a
        traceback instead of the JSON envelope the proxy expects."""
        for bad in (None, 42, b"/bytes/path", object()):
            resolved, err = resolve_under_repos(bad)
            assert resolved is None
            assert err

    def test_refuses_an_intermediate_symlink_out(self, repos, tmp_path):
        """The escape does not have to be the leaf. An argument whose *middle*
        component is a link out would survive an implementation that only
        inspected the last element."""
        outside = tmp_path / "outside-tree"
        (outside / "sub").mkdir(parents=True)
        midlink = repos / "ns" / "midlink"
        midlink.symlink_to(outside)
        resolved, err = resolve_under_repos(str(midlink / "sub"))
        assert resolved is None
        assert err and "outside" in err.lower()


class TestDeveloperReposRootSanity:
    """`DEVELOPER_REPOS_DIR` is derived from operator config, which validates
    it nowhere. Refusing an unset variable and then accepting `/` would not be
    a boundary.

    Each case here names the guard it is aimed at, because the user-id check
    added for the per-user split would refuse most of these on its own and a
    guard tested only through another guard is a guard nobody would notice
    losing.
    """

    def test_refuses_filesystem_root(self, monkeypatch):
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "/")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None
        resolved, err = resolve_under_repos("/etc")
        assert resolved is None
        assert err

    def test_refuses_a_single_component_root(self, monkeypatch):
        """Aimed at the depth guard alone: `/alice` is the subtree `alice`
        names, so the user-id check passes and only the depth refuses it."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "/alice")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    def test_refuses_a_relative_root(self, monkeypatch):
        """A relative value anchors on wherever the CLI was started."""
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", "relative/repos/alice")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() is None

    def test_accepts_an_ordinary_root(self, tmp_path, monkeypatch):
        root = tmp_path / "srv" / "repos" / "alice"
        root.mkdir(parents=True)
        monkeypatch.setenv("DEVELOPER_REPOS_DIR", str(root))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert developer_repos_root() == root.resolve()


class TestUserWorkspaceRoot:
    """The one root a caller may *derive a destination inside*.

    `browse screenshot` needs it by name rather than by list position: the
    first entry of `allowed_host_roots` is usually the task's deferred temp
    dir, which nothing serves and the scheduler sweeps.
    """

    def test_is_the_callers_own_subtree(self, mount):
        assert user_workspace_root() == (mount / "Users" / "alice").resolve()

    def test_is_none_without_a_user_id(self, mount, monkeypatch):
        monkeypatch.delenv("ISTOTA_USER_ID")
        assert user_workspace_root() is None

    def test_is_none_without_a_mount(self, mount, monkeypatch):
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH")
        assert user_workspace_root() is None

    @pytest.mark.parametrize("user_id", ["", ".", "..", "/etc", "../bob", "a/b"])
    def test_a_user_id_that_does_not_name_a_child_gets_no_root(
        self, mount, monkeypatch, user_id,
    ):
        """The collapsed join is `{mount}/Users` — every user at once."""
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        assert user_workspace_root() is None

    def test_allowed_host_roots_uses_the_same_derivation(self, mount):
        """One derivation, so a derived destination cannot land somewhere the
        allowlist would then refuse — or, worse, somewhere it would not."""
        assert user_workspace_root() in allowed_host_roots(writable=True)


class TestALeafSymlinkIsRefusedAsADestination:
    """The parent is resolved and contained; the final component is not.

    `resolved` is `resolved_parent / name`, so a link standing at that name
    passes containment as a *name* and is then followed by whatever opens it —
    `cp-out`'s `write_bytes`, the OPML exporter's own open. The tree is bound
    read-write into the sandbox, so the link is model-plantable.
    """

    def test_refused(self, mount, tmp_path):
        outside = tmp_path / "outside.txt"
        link = mount / "Users" / "alice" / "export.csv"
        link.symlink_to(outside)
        resolved, err = resolve_host_path(
            link, writable=True, operation="export",
        )
        assert resolved is None
        assert err is not None and "symlink" in err
        assert not outside.exists()

    def test_a_link_that_stays_inside_the_root_is_refused_too(self, mount):
        """No consumer writes through a link on purpose, and telling the two
        apart at check time does not survive the link being repointed."""
        target = mount / "Users" / "alice" / "real.csv"
        target.write_text("x")
        link = mount / "Users" / "alice" / "alias.csv"
        link.symlink_to(target)
        _, err = resolve_host_path(link, writable=True, operation="export")
        assert err is not None

    def test_an_ordinary_existing_file_is_still_an_allowed_destination(self, mount):
        """The overwrite case, which the refusal above must not take with it."""
        target = mount / "Users" / "alice" / "real.csv"
        target.write_text("x")
        resolved, err = resolve_host_path(target, writable=True, operation="export")
        assert err is None
        assert resolved == target.resolve()


class TestWriteResolved:
    def test_writes_the_bytes(self, mount):
        dest = mount / "Users" / "alice" / "out.bin"
        write_resolved(dest, b"hello")
        assert dest.read_bytes() == b"hello"

    def test_truncates_rather_than_appending(self, mount):
        dest = mount / "Users" / "alice" / "out.bin"
        dest.write_bytes(b"aaaaaaaaaa")
        write_resolved(dest, b"bb")
        assert dest.read_bytes() == b"bb"

    def test_refuses_to_follow_a_symlink_planted_after_the_check(
        self, mount, tmp_path,
    ):
        """The window `resolve_host_path` cannot close on its own.

        The check refused a link as of its own moment; this is what happens
        when one appears between that moment and the open.
        """
        outside = tmp_path / "victim.txt"
        outside.write_text("original")
        link = mount / "Users" / "alice" / "out.bin"
        link.symlink_to(outside)
        with pytest.raises(OSError):
            write_resolved(link, b"overwritten")
        assert outside.read_text() == "original"


class TestBrowseScreenshotIsScoped:
    """`--output` was an unguarded host write, and the default was worse.

    The old default was `/tmp/screenshot.png`. The CLI runs host-side through
    the proxy while the model's `/tmp` is the sandbox's own tmpfs, so the file
    landed on the host and the model was handed a path it could not open.

    Every case here asserts on the filesystem as well as on the envelope. The
    ordering bug this module already carries a test for created the directory
    and *then* refused, which a test reading the return value alone cannot see.
    """

    @pytest.fixture
    def post(self):
        """`httpx.post` answering with a real PNG, and a record of the calls."""
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b"\x89PNG\r\n\x1a\n" + b"pretend pixels"
        with patch("istota.skills.browse.httpx.post", return_value=resp) as m:
            yield m

    def _shot(self, *argv):
        from istota.skills.browse import build_parser, cmd_screenshot
        return cmd_screenshot(build_parser().parse_args(["screenshot", *argv]))

    def test_an_output_outside_the_workspace_is_refused(self, mount, tmp_path, post):
        dest = tmp_path / "attacker" / "deep" / "shot.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert not dest.exists()
        # The mkdir must never have run: an out-of-bounds tree created as the
        # daemon user is a write, whatever the envelope then says.
        assert not (tmp_path / "attacker").exists()
        # And nothing was captured, so a refusal costs no browser time.
        post.assert_not_called()

    def test_another_users_workspace_is_refused(self, mount, post):
        dest = mount / "Users" / "bob" / "shot.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert not dest.exists()
        post.assert_not_called()

    def test_an_output_inside_the_workspace_is_written(self, mount, post):
        dest = mount / "Users" / "alice" / "shots" / "page.png"
        result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "ok"
        assert dest.read_bytes().startswith(b"\x89PNG")
        assert result["media_type"] == "image/png"

    def test_the_default_lands_in_the_callers_own_workspace(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        result = self._shot("https://example.com")
        assert result["status"] == "ok"
        written = Path(result["path"])
        expected_dir = (mount / "Users" / "alice" / "istota" / "screenshots").resolve()
        assert written.parent == expected_dir
        assert written.read_bytes().startswith(b"\x89PNG")
        # The `?path=` spelling `/chat/files` takes, so a reply can embed the
        # picture without rebuilding the path by hand.
        assert result["workspace_path"] == (
            "/Users/alice/istota/screenshots/" + written.name
        )

    def test_the_default_follows_the_configured_bot_dir(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "mister_jones")
        result = self._shot("https://example.com")
        assert Path(result["path"]).parent.name == "screenshots"
        assert Path(result["path"]).parent.parent.name == "mister_jones"

    def test_a_bot_dir_that_is_not_a_plain_component_is_refused(
        self, mount, monkeypatch, post,
    ):
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "../../etc")
        result = self._shot("https://example.com")
        assert result["status"] == "error"
        post.assert_not_called()

    def test_no_output_and_no_workspace_refuses_rather_than_falling_back(
        self, monkeypatch, post,
    ):
        """The old fallback was `/tmp/screenshot.png`, which is the bug."""
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        result = self._shot("https://example.com")
        assert result["status"] == "error"
        assert not Path("/tmp/screenshot.png").exists()
        post.assert_not_called()

    def test_a_body_that_is_not_a_raster_is_not_written(self, mount):
        """A 200 labelled `image/png` over an HTML error page.

        Same predicate `/chat/files` sniffs the file with, so a capture that
        would come back as a download rather than an image is refused here
        instead of being embedded as a broken one.
        """
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b"<html><body>upstream error</body></html>"
        dest = mount / "Users" / "alice" / "shot.png"
        with patch("istota.skills.browse.httpx.post", return_value=resp):
            result = self._shot("https://example.com", "-o", str(dest))
        assert result["status"] == "error"
        assert not dest.exists()


class TestHealthExportCsvIsScoped:
    """`--output` was an arbitrary host write with a whole health record in it."""

    def _export(self, output):
        import argparse

        from istota.skills.health import cmd_export_csv

        return cmd_export_csv(argparse.Namespace(output=str(output)))

    def test_a_path_outside_the_workspace_is_refused_before_the_database(
        self, mount, tmp_path, capsys,
    ):
        """No `HEALTH_DB_PATH` is set here, so reaching `_connect` would fail
        with a different message — which is what shows the refusal came first.
        The export is the caller's entire health record; a refusal should not
        read it out of the database on the way to saying no."""
        dest = tmp_path / "attacker" / "panels.csv"
        with pytest.raises(SystemExit) as exc:
            self._export(dest)
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "outside allowed roots" in payload["error"]
        assert not dest.exists()
        assert not (tmp_path / "attacker").exists()

    def test_another_users_workspace_is_refused(self, mount, capsys):
        dest = mount / "Users" / "bob" / "panels.csv"
        with pytest.raises(SystemExit):
            self._export(dest)
        # The payload, not the exit: with the guard removed this verb still
        # raises SystemExit, from `_db_path` finding no HEALTH_DB_PATH. A test
        # reading only the exception passes against an unguarded verb, which is
        # what the control found.
        payload = json.loads(capsys.readouterr().out)
        assert "outside allowed roots" in payload["error"]
        assert not dest.exists()


class TestFeedsOpmlIsScoped:
    """One read and one write, and both go to a CLI that opens the path itself.

    So the *resolved* path is what has to travel: handing the Click CLI the
    original argument re-walks every symlink the check just settled.
    """

    @pytest.fixture
    def ran(self):
        from unittest.mock import patch

        with patch(
            "istota.skills.feeds._run", return_value={"status": "ok"},
        ) as m:
            yield m

    def test_import_outside_the_workspace_is_refused(
        self, mount, tmp_path, capsys, ran,
    ):
        import argparse

        from istota.skills.feeds import cmd_import_opml

        source = tmp_path / "elsewhere" / "subs.opml"
        source.parent.mkdir()
        source.write_text("<opml/>")
        with pytest.raises(SystemExit) as exc:
            cmd_import_opml(argparse.Namespace(path=str(source)))
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"
        ran.assert_not_called()

    def test_export_outside_the_workspace_is_refused_and_creates_nothing(
        self, mount, tmp_path, capsys, ran,
    ):
        import argparse

        from istota.skills.feeds import cmd_export_opml

        dest = tmp_path / "attacker" / "deep" / "subs.opml"
        with pytest.raises(SystemExit) as exc:
            cmd_export_opml(argparse.Namespace(output=str(dest)))
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"
        assert not dest.exists()
        assert not (tmp_path / "attacker").exists()
        ran.assert_not_called()

    def test_the_resolved_path_is_what_reaches_the_cli(self, mount, ran):
        """The path handed down must be the resolved one, not the argument.

        Reached through a symlinked intermediate directory so the two strings
        genuinely differ: a test where they happen to be equal cannot tell a
        resolving implementation from a passthrough, and on a host whose temp
        directory is already a realpath they are equal for every plain path.
        """
        import argparse

        from istota.skills.feeds import cmd_export_opml

        real = mount / "Users" / "alice" / "real" / "exports"
        real.mkdir(parents=True)
        (mount / "Users" / "alice" / "via").symlink_to(
            mount / "Users" / "alice" / "real",
        )
        dest = mount / "Users" / "alice" / "via" / "exports" / "subs.opml"
        assert str(dest) != str(dest.resolve())

        cmd_export_opml(argparse.Namespace(output=str(dest)))
        ran.assert_called_once()
        assert ran.call_args[0][0] == [
            "export-opml", "--output", str(real.resolve() / "subs.opml"),
        ]

    def test_export_inside_the_workspace_is_allowed(self, mount, ran):
        import argparse

        from istota.skills.feeds import cmd_export_opml

        cmd_export_opml(argparse.Namespace(output=str(
            mount / "Users" / "alice" / "exports" / "subs.opml",
        )))
        ran.assert_called_once()

    def test_import_inside_the_workspace_is_allowed(self, mount, ran):
        import argparse

        from istota.skills.feeds import cmd_import_opml

        real = mount / "Users" / "alice" / "real"
        real.mkdir()
        (real / "subs.opml").write_text("<opml/>")
        (mount / "Users" / "alice" / "via").symlink_to(real)
        source = mount / "Users" / "alice" / "via" / "subs.opml"
        assert str(source) != str(source.resolve())

        cmd_import_opml(argparse.Namespace(path=str(source)))
        ran.assert_called_once()
        # Resolved, again: reopening the argument re-walks `via`.
        assert ran.call_args[0][0] == [
            "import-opml", str((real / "subs.opml").resolve()),
        ]
