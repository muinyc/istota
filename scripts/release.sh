#!/usr/bin/env bash
# Cut a new release: move CHANGELOG [Unreleased] to a versioned section,
# bump pyproject.toml, commit, tag with the section as the annotation body,
# push to origin. The GitHub mirror's release.yml workflow creates the
# GitHub Release from the tag annotation.
#
# The [Unreleased] section must open with the release announcement: prose
# before the first ### heading, which both extractors emit ahead of the
# changeset. Write it last, immediately before cutting. How to write one:
# .claude/rules/releases.md.
#
# Usage: scripts/release.sh 0.7.0 [--no-announcement]

set -euo pipefail

REQUIRE_ANNOUNCEMENT=1
NEW=""
for arg in "$@"; do
  case "$arg" in
    --no-announcement) REQUIRE_ANNOUNCEMENT=0 ;;
    -*) echo "unknown option: $arg" >&2; exit 1 ;;
    *) NEW="$arg" ;;
  esac
done
: "${NEW:?version required, e.g. 0.7.0}"
DATE=$(date +%Y-%m-%d)
TAG="v$NEW"
REPO_URL="https://gitlab.com/cynium/istota"

if ! git diff-index --quiet HEAD --; then
  echo "working tree dirty — commit or stash first" >&2
  exit 1
fi
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "tag $TAG already exists" >&2
  exit 1
fi
if ! grep -q '^## \[Unreleased\]$' CHANGELOG.md; then
  echo "no [Unreleased] section in CHANGELOG.md" >&2
  exit 1
fi

# The announcement is the prose between the [Unreleased] heading and the first
# ### subsection. A cut without one publishes hundreds of bullets and no summary.
if [ "$REQUIRE_ANNOUNCEMENT" = 1 ]; then
  python3 - <<'ANNOUNCEMENT_CHECK'
import re, sys, pathlib

CANONICAL = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}

text = pathlib.Path("CHANGELOG.md").read_text()
m = re.search(r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
section = m.group(1) if m else ""
preamble = re.split(r"^### ", section, maxsplit=1, flags=re.M)[0].strip()

if len(preamble) < 200:
    sys.exit(
        "no release announcement in the [Unreleased] section.\n"
        "Write it as prose between '## [Unreleased]' and the first '### ' heading.\n"
        "How: .claude/rules/releases.md. What one looks like: the announcement under\n"
        "the most recent version in CHANGELOG.md. Pass --no-announcement for a hotfix\n"
        "that genuinely needs none."
    )

# A heading inside the announcement does not stay inside it. Both extractors
# split the section on '### ', so one is read as a changeset subsection and
# emitted after the real ones; a '#' or '##' stays put and outranks the
# version heading it sits under.
stray = [h for h in re.findall(r"^### (.+)$", section, re.M) if h.strip() not in CANONICAL]
stray += re.findall(r"^#{1,2} .+$", preamble, re.M)
if stray:
    sys.exit(
        "headings in the [Unreleased] section that the extractors will misplace:\n"
        + "\n".join("  " + s for s in stray)
        + "\nThe announcement is prose — use paragraphs and bold lead-ins, not headings."
    )
ANNOUNCEMENT_CHECK
fi

# Move [Unreleased] → [NEW] - DATE, add fresh empty [Unreleased] above,
# update link references at bottom.
python3 - "$NEW" "$DATE" "$REPO_URL" <<'PY'
import re, sys, pathlib
new, date, url = sys.argv[1:]
path = pathlib.Path("CHANGELOG.md")
text = path.read_text()
text = text.replace(
    "## [Unreleased]",
    f"## [Unreleased]\n\n## [{new}] - {date}",
    1,
)
old_unrel = re.search(r"^\[Unreleased\]:.*$", text, re.M).group(0)
new_unrel = f"[Unreleased]: {url}/-/compare/v{new}...main"
new_link = f"[{new}]: {url}/-/releases/v{new}"
text = text.replace(old_unrel, f"{new_unrel}\n{new_link}")
path.write_text(text)
PY

# Bump pyproject.toml version (canonical source; src/istota/__init__.py
# derives __version__ from package metadata at runtime).
sed -i.bak -E "s/^version = \".*\"/version = \"$NEW\"/" pyproject.toml
rm pyproject.toml.bak

# Reconcile the lockfile to the bumped version so the tagged tree isn't born
# dirty (the editable root package pins its own version in uv.lock).
if command -v uv >/dev/null 2>&1; then
  uv lock --quiet
fi

# Bump the dev-mode mock backend so VITE_MOCK_API=1 reports the new version.
if [ -f web/vite-mock-api.ts ]; then
  sed -i.bak -E "s/^([[:space:]]+version: ')[^']*(',)/\\1$NEW\\2/" web/vite-mock-api.ts
  rm web/vite-mock-api.ts.bak
fi

# Extract just-published version's section as the tag body, consolidating
# duplicate ### subsections (Added/Changed/Fixed/...) into one of each in
# Keep-a-Changelog order. The CHANGELOG file itself is left chronological.
NOTES=$(python3 - "$NEW" <<'PY'
import re, sys, pathlib
version = sys.argv[1]
text = pathlib.Path("CHANGELOG.md").read_text()

m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
if not m:
    sys.exit(f"section for {version} not found")
section = m.group(1)

# Split into (header, body) chunks. Content before the first ### (if any) is
# kept under a None bucket and emitted first.
parts = re.split(r"^(### .+)$", section, flags=re.M)
buckets: dict[str | None, list[str]] = {}
order: list[str | None] = []

def add(key, body):
    body = body.strip("\n")
    if not body.strip():
        return
    if key not in buckets:
        buckets[key] = []
        order.append(key)
    buckets[key].append(body)

# parts[0] is preamble; then alternating header, body
add(None, parts[0])
for i in range(1, len(parts), 2):
    add(parts[i].strip(), parts[i + 1])

CANONICAL = ["### Added", "### Changed", "### Deprecated", "### Removed", "### Fixed", "### Security"]
ordered_keys = [None] + [h for h in CANONICAL if h in buckets] + [k for k in order if k not in CANONICAL and k is not None]

out = []
for key in ordered_keys:
    if key not in buckets:
        continue
    if key is not None:
        out.append(key)
        out.append("")
    out.append("\n\n".join(buckets[key]))
    out.append("")
print("\n".join(out).strip("\n"))
PY
)

if [ -z "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ]; then
  echo "extracted release notes for $TAG are empty" >&2
  exit 1
fi

git add CHANGELOG.md pyproject.toml
# Stage the reconciled lockfile too (only changes when uv is present).
[ -f uv.lock ] && git add uv.lock || true
# vite-mock-api.ts is bumped above only when present; stage it the same way so
# the version bump lands in this commit instead of leaving the tree dirty.
[ -f web/vite-mock-api.ts ] && git add web/vite-mock-api.ts || true
git commit -m "Bump version to $NEW"
git tag -a "$TAG" --cleanup=verbatim -m "Release $TAG" -m "$NOTES"
git push --follow-tags

echo
echo "Released $TAG to origin."
echo "GitHub Release will be created when the mirror picks up the tag."
