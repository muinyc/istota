"""Host-path allowlist shared by the skill CLIs that take one.

A skill CLI does not run in the sandbox. The proxy spawns it host-side, with
the daemon's filesystem view, precisely so it can reach the databases the model
cannot. That makes any verb accepting a *host* path an arbitrary-file read or
write unless it is scoped — and the model chooses the path.

There are two allowlists here, for two kinds of path.

``resolve_host_path`` scopes a path inside the caller's own workspace against
the mount roots below. Its consumers are devbox's ``cp-in`` / ``cp-out``, ``kv
set --value-file``, email's outbound ``--attach``, ``browse screenshot
--output``, ``health export-csv --output`` and both ``feeds`` OPML verbs;
``scheduler_deferred`` applies the same rule to deferred health-op paths.
``tests/test_skill_host_paths_coverage.py`` walks every skill's argparse tree
and requires each host-path argument to be registered as scoped or, where it is
not, as a recorded gap — a hand-maintained list of consumers goes stale in
silence, and the arguments that were never added to it are the whole problem.

``resolve_under_repos`` scopes a *worktree* against ``DEVELOPER_REPOS_DIR``,
which is somewhere else entirely and is bound into the sandbox for admins only.
Its consumer is the ``code_review`` CLI. The two live in one module so neither
the roots nor the error convention can drift apart, but they are separate
allowlists and a path admitted by one is not admitted by the other.

The rule lives here rather than in any skill: a stdlib-only leaf module,
importable from a skill subprocess without dragging in the framework.

**The roots mirror what the sandbox binds, per user.** `NEXTCLOUD_MOUNT_PATH`
is deliberately the *shared* mount root for everyone — every consumer builds
`$NEXTCLOUD_MOUNT_PATH/Users/<uid>/…` itself, and per-user isolation comes from
`build_bwrap_cmd` binding only the caller's own subtree plus each CLI
self-scoping by `ISTOTA_USER_ID`. A host-side path argument does neither, so
taking the mount root as a root would hand back any other user's workspace
through `kv get`. The roots here are therefore the same three the sandbox
binds: the task's deferred dir, `{mount}/Users/{ISTOTA_USER_ID}`, and the
task's own `{mount}/Channels/{ISTOTA_CONVERSATION_TOKEN}` — plus `{mount}/Talk`
for reads only, matching its read-only bind. With no `ISTOTA_USER_ID` in the
environment the mount contributes nothing; the deferred dir stands alone. An
`ISTOTA_USER_ID` that is set but does not name a child of `{mount}/Users` costs
that one root and no other — the channel and Talk roots are scoped by the
conversation token and by nothing, not by the user id — because the collapsed
join is `{mount}/Users`, every user's directory at once (ISSUE-402).

`DEVELOPER_REPOS_DIR` is scoped the same way, and it did not used to be. It
named the whole of `developer.repos_dir`, one tree shared by every admin, so
`code_review --worktree <another admin's checkout>` passed containment and came
back as reviewer-prompt text. The variable is now the caller's own subtree and
`developer_repos_root` re-derives that scope from `ISTOTA_USER_ID` rather than
trusting it.

**Callers must use the returned resolved path.** Validating one path and then
opening the original re-walks every symlink in it, so a link swapped in between
lands outside the allowlist with the check already passed. The resolved path
has no symlink components *as of the check*, which removes that re-walk.

It does not make the caller's later open atomic. Both allowlists validate a
path, and the trees they validate are bound read-write into the sandbox, so a
component can in principle be replaced between the return and the use. Path
validation cannot close that on its own. What it does close is the much larger
window of re-resolving an attacker-supplied string, which is why the rule is to
operate on what comes back and never to re-walk the argument.
"""

import os
from pathlib import Path

from .user_scope import scoped_user_dir


def user_workspace_root() -> Path | None:
    """`{NEXTCLOUD_MOUNT_PATH}/Users/{ISTOTA_USER_ID}`, or None.

    The one root a skill CLI can *derive a destination inside* rather than
    merely validate one against, which is why it is a function of its own
    rather than `allowed_host_roots()[n]`: that list is ordered by how the
    roots were added, its first entry is usually the task's deferred dir — a
    temp directory nothing serves and the scheduler sweeps — and a caller
    picking an element out of it would be choosing a destination by list
    position. `browse screenshot` needs this one specifically, because the
    file it writes has to be somewhere the task can read back *and*
    `/chat/files` can serve, and only the workspace is both.

    Scoped exactly as `allowed_host_roots` scopes it, by calling this — one
    derivation, so a default destination cannot land somewhere the allowlist
    would then refuse, or (worse) somewhere it would not. None when either
    variable is unset or blank, and None when the user id does not name a
    child of `{mount}/Users`: the collapsed join is `{mount}/Users` itself,
    every user's directory at once (ISSUE-402).
    """
    mount_raw = os.environ.get("NEXTCLOUD_MOUNT_PATH", "").strip()
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if not mount_raw or not user_id:
        return None
    try:
        mount = Path(mount_raw).resolve()
    except OSError:
        return None
    return scoped_user_dir(mount / "Users", user_id)


def allowed_host_roots(*, writable: bool = False) -> list[Path]:
    """Host directories a skill CLI may read from (or write to).

    `writable=True` drops the roots the sandbox binds read-only, so a
    destination path can't be steered into shared, non-user-owned storage.
    """
    roots: list[Path] = []

    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "").strip()
    if deferred:
        try:
            roots.append(Path(deferred).resolve())
        except OSError:
            pass

    mount_raw = os.environ.get("NEXTCLOUD_MOUNT_PATH", "").strip()
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if mount_raw and user_id:
        try:
            mount = Path(mount_raw).resolve()
        except OSError:
            return roots
        # Scoped the same way the token below already was. The two sat side by
        # side with only the token guarded, so a `.` or an absolute
        # `ISTOTA_USER_ID` collapsed this root to `{mount}/Users` — every
        # user's directory, as a destination a skill CLI may write to
        # (ISSUE-402). No root rather than a wider one; `resolve_host_path`
        # refuses everything when the list comes back empty.
        own = user_workspace_root()
        if own is not None:
            roots.append(own)
        token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "").strip()
        # Guard the token the same way the container name is guarded: it lands
        # in a path, and "../.." would walk straight back out of Channels/.
        if token and "/" not in token and token not in (".", ".."):
            roots.append(mount / "Channels" / token)
        if not writable:
            # Talk attachments are bound read-only in the sandbox; a read may
            # reach them, a destination may not.
            roots.append(mount / "Talk")
    return roots


def resolve_host_path(
    path: Path, *, writable: bool, operation: str,
) -> tuple[Path | None, str | None]:
    """Validate `path` against the allowlist and return the path to actually use.

    Returns `(resolved, None)` on success or `(None, error)` on refusal.
    **Use the returned path**, not the one passed in — see the module docstring.

    `writable=False` means an existing source to read; `writable=True` means a
    destination that need not exist yet.
    """
    roots = allowed_host_roots(writable=writable)
    if not roots:
        # No allowlist resolvable — a CLI smoke test outside the executor, or a
        # misconfigured deployment. Refuse rather than silently widening the
        # boundary to the whole filesystem.
        return None, (
            f"No allowed host roots configured (ISTOTA_DEFERRED_DIR / "
            f"NEXTCLOUD_MOUNT_PATH + ISTOTA_USER_ID unset). {operation} refused."
        )

    try:
        if not writable:
            if path.is_symlink():
                return None, f"Refusing host-side symlink: {path}"
            if not path.exists():
                return None, f"Path not found: {path}"
            resolved = path.resolve(strict=True)
        else:
            # A destination need not exist yet, so anchor on the parent. Resolve
            # and check it *before* creating anything — the old order mkdir'd an
            # out-of-bounds tree as the daemon user and only then refused.
            parent = path.parent
            if parent.is_symlink():
                return None, f"Refusing host-side symlink on dest parent: {parent}"
            resolved_parent = parent.resolve()
            if not _under_a_root(resolved_parent, roots):
                return None, _outside(resolved_parent, roots)
            resolved = resolved_parent / path.name
            # The leaf, and it is not covered by anything above. The parent is
            # resolved and contained, so `resolved` is inside a root *as a
            # name* — but a symlink standing at that name is not resolved by
            # the join, and every writer here opens by name: `cp-out` does
            # `dest.write_bytes`, `export-opml` hands the path to a CLI that
            # opens it. Both follow the link and land wherever it points, with
            # containment already reported as passed. The tree is bound
            # read-write into the sandbox, so such a link is model-plantable.
            # Refusing it does not make the later open atomic — a caller that
            # opens the result should still pass `O_NOFOLLOW`, which is what
            # the module docstring means by the window path validation cannot
            # close on its own — but it is what turns "wrote outside the
            # allowlist" into a race rather than a one-liner.
            if resolved.is_symlink():
                return None, f"Refusing host-side symlink as destination: {resolved}"
            parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"Path resolution failed: {e}"

    if not _under_a_root(resolved, roots):
        return None, _outside(resolved, roots)
    return resolved, None


def write_resolved(path: Path, data: bytes) -> None:
    """Write `data` to a path `resolve_host_path` just returned, refusing a link.

    The counterpart to "use the returned resolved path": the caller still has
    to *open* it, and a plain `open(path, "wb")` follows a symlink standing at
    the final component. `resolve_host_path` refuses one as of its own check,
    which leaves the window between the check and this open — narrow, and
    model-plantable, since the workspace is bound read-write into the sandbox.
    `O_NOFOLLOW` closes it: the open fails rather than landing wherever the
    link points, as the daemon user.

    `O_TRUNC` rather than `O_EXCL`, because overwriting a destination the
    caller named is what every consumer here already did. The mode is what a
    plain `open` produces once the umask is applied, so a file written this way
    is not narrower than the workspace's other files — the web process serving
    it back through `/chat/files` has to be able to read it.

    Raises `OSError`, which every consumer already handles: a refusal is an
    envelope, not a traceback.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    try:
        handle = os.fdopen(fd, "wb")
    except BaseException:
        # Only this call. Past it the descriptor belongs to `handle`, and
        # closing it a second time would close whatever number was reused.
        os.close(fd)
        raise
    with handle:
        handle.write(data)


def validate_host_path(
    path: Path, *, must_exist: bool, operation: str,
) -> str | None:
    """Error-only wrapper for callers that don't need the resolved path.

    Prefer `resolve_host_path`: reading the unresolved path reopens the symlink
    window this check exists to close.
    """
    _, err = resolve_host_path(path, writable=not must_exist, operation=operation)
    return err


def developer_repos_root() -> Path | None:
    """The calling task's own subtree of the developer repos tree, or None.

    Separate from `allowed_host_roots` on purpose. The mount roots scope a path
    the model names inside its own workspace; this scopes a *worktree* the model
    names, which lives somewhere else entirely. Mixing the two would let a
    review read the user's workspace and a `kv --value-file` read a checkout.

    **The root is one user's subtree, and that is checked here rather than
    assumed.** `developer.repos_dir` is a root of per-user subtrees; the
    developer skill's `setup_env` derives `DEVELOPER_REPOS_DIR` as
    `{repos_dir}/{ISTOTA_USER_ID}` and `build_bwrap_cmd` binds the same path.
    While the variable named the shared root, `resolve_under_repos` admitted
    another admin's checkout — and that path runs *host-side*, outside the
    sandbox, so none of the bwrap-side scoping reached it. Re-deriving the
    scope from `ISTOTA_USER_ID` is what the rest of this module does with the
    mount root, and it means a variable that regresses to the shared root is
    refused here instead of quietly widening containment to every user's tree.

    Two checks, mirroring `executor.get_user_repos_dir`. The name as written
    must be the user id, which refuses a root one level too high or one naming
    somebody else; and the resolved path must equal the resolved *parent* plus
    that name, which refuses a symlink standing where the subtree should be.
    The tree was bound read-write and shared for as long as the old layout
    stood, so such a link is model-plantable rather than hypothetical. The
    second is deliberately not a second `.name` test — a planted link would be
    named for its victim, so `{repos_dir}/alice -> /anywhere/alice` resolves to
    a name that still passes one.

    **What it does not check** is that the parent is `developer.repos_dir`, so
    `/anywhere/at/all/alice` passes. It cannot: this is a stdlib-only leaf with
    no access to the loaded config, which is why `executor.get_user_repos_dir`
    — which does know the configured root — is the seam that spells the whole
    equality out. Read `{repos_dir}/{ISTOTA_USER_ID}` above as the shape this
    checks *against*, not as an equality enforced here.

    One consequence is worth naming because it looks like a bug and is not
    fixable from here: where the user id happens to equal the last component of
    `developer.repos_dir` (`repos_dir = /srv/dev`, user `dev`), the shared root
    and that user's subtree are indistinguishable to this function and the
    shared root is accepted. `/srv/dev` is a legitimate per-user root for `dev`
    under `repos_dir = /srv`, and nothing in the environment says which. The
    boundary against that is upstream, where the value is derived. Nothing
    reaches this today that could steer it: the variable is daemon-set and is
    never model-supplied.

    None when the variable is unset or blank, when `ISTOTA_USER_ID` is unset or
    blank, or when either check fails. Refusing is this module's posture for a
    root it cannot resolve, and an unscoped root is one it cannot resolve.

    **This resolves the root; it does not decide who may use it.** A non-`None`
    return says nothing about authorization; the `is_admin` check belongs to the
    calling CLI.
    """
    raw = os.environ.get("DEVELOPER_REPOS_DIR", "").strip()
    if not raw:
        return None
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if not user_id:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None

    # A relative value would anchor on wherever the CLI happened to be started,
    # which is not a boundary anyone chose.
    if not Path(raw).is_absolute():
        return None
    # `/` passes every containment check there is. Refusing an unset variable
    # "rather than widening to the whole filesystem" and then accepting the one
    # value that widens to the whole filesystem is not a boundary. Two
    # components is the shallowest plausible real root (`/srv/alice`).
    if len(root.parts) < 3:
        return None
    # Two checks, and the second is structural rather than a second name
    # comparison. The name as written must be the user id — that refuses a root
    # one level too high, or one naming somebody else. Then the *resolved* path
    # must be the child that name denotes inside the resolved parent, which is
    # `executor.get_user_repos_dir`'s rule with the one term this module cannot
    # know (`developer.repos_dir`) replaced by the parent it was given. A
    # second `.name` test would not do: `{repos_dir}/alice -> /anywhere/alice`
    # resolves to a path whose name is still `alice`, and a planted link would
    # of course be named for its victim.
    #
    # Resolving the parent separately is what keeps a symlinked deployment root
    # (`/srv/repos` -> `/data/repos`) working: that link is above the user
    # component, so both sides resolve through it and agree.
    if Path(raw).name != user_id:
        return None
    try:
        if Path(raw).parent.resolve() / user_id != root:
            return None
    except (OSError, ValueError):
        return None
    return root


def resolve_under_repos(path: str | Path) -> tuple[Path | None, str | None]:
    """Validate a worktree directory against the caller's own repos subtree.

    The root is whatever `developer_repos_root` returns, which is
    `{developer.repos_dir}/{ISTOTA_USER_ID}` — so another user's checkout is
    outside it and refused. Nothing in the body changed when the layout split;
    containment has always been against whatever that function answers, which
    is why the fix belongs there and not here.

    Returns `(resolved, None)` on success or `(None, error)` on refusal, the
    same shape as `resolve_host_path` — one module, one error convention, and
    the caller has to turn the failure into a JSON envelope either way.

    **The symlink rule differs from `resolve_host_path`, deliberately.** That
    one refuses a symlinked argument outright. This one follows links and then
    checks containment, because a worktree is legitimately reached through one
    and refusing would break ordinary layouts. A link that stays inside the root
    is therefore accepted; one that leaves it resolves outside and is refused.
    Following is what catches the escape, so it has to happen before the check.

    **Use the returned path**, and do not re-walk the argument — see the module
    docstring for what that does and does not guarantee.

    **Containment is not sufficient to make a git invocation on the result
    safe.** A path fully inside the root can still be a repository that runs
    code or reads outside it, because `repos_dir` is bound read-write into the
    sandbox and a repository's behaviour lives in files the model can write:

    - `.git/config` in a contained worktree can set `diff.external`,
      `core.fsmonitor` or a textconv filter, each of which makes `git diff`
      execute an arbitrary command as the daemon user.
    - A plain-directory argument makes git search *upward* for a repository, so
      it can operate on one above the root.
    - A `.git` file containing `gitdir: <outside>` redirects the repository out
      of the root while `rev-parse --show-toplevel` still reports the contained
      path.

    Callers must therefore neutralise repository-supplied configuration and
    confirm the resolved git directory, not just the worktree path. See the
    `code_review` CLI for the invocation that does this; do not call git on the
    result of this function without it.
    """
    root = developer_repos_root()
    if root is None:
        return None, (
            "No developer repos root resolved for this task, so no worktree "
            "path can be validated. DEVELOPER_REPOS_DIR must be set and must "
            "name this task's own subtree (ISTOTA_USER_ID). Refusing rather "
            "than widening to the whole filesystem."
        )

    # `Path("")` is `.`, which would resolve to wherever the daemon happens to
    # have been started. Refuse explicitly rather than letting the outcome
    # depend on the process CWD.
    if not isinstance(path, (str, os.PathLike)):
        # The contract is to return an error, never to raise. `Path(None)` is a
        # TypeError, and `args.worktree` is None whenever the flag is omitted.
        return None, f"Invalid worktree path of type {type(path).__name__}"

    raw = str(path).strip()
    if not raw:
        return None, "Empty worktree path"

    try:
        candidate = Path(raw)
        if not candidate.exists():
            return None, f"Path not found: {candidate}"
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            return None, f"Not a directory: {resolved}"
    except (OSError, ValueError) as e:
        # ValueError covers an embedded NUL, which `exists()` raises rather than
        # returning False on some versions.
        return None, f"Path resolution failed: {e}"

    if not _under_a_root(resolved, [root]):
        return None, (
            f"Path {resolved} is outside the developer repos root ({root})"
        )
    return resolved, None


def _under_a_root(resolved: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _outside(resolved: Path, roots: list[Path]) -> str:
    return (
        f"Path {resolved} is outside allowed roots "
        f"({', '.join(str(r) for r in roots)})"
    )
