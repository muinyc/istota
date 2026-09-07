# Cutting a release, and the announcement that opens one

`scripts/release.sh 0.41.0` does the mechanics: moves `## [Unreleased]` to `## [0.41.0] - DATE`, opens a fresh empty `[Unreleased]`, bumps `pyproject.toml`, reconciles `uv.lock` and `web/vite-mock-api.ts`, commits, tags with `--cleanup=verbatim` so the `###` headings survive, and pushes with `--follow-tags` to both remotes. `.github/workflows/release.yml` then builds the GitHub Release from `CHANGELOG.md` in the checked-out tag — never from the tag annotation, which `actions/checkout` does not reliably fetch as an object. Neither the annotation nor the release body is the chronological file: both extractors consolidate duplicate `### Added` / `### Changed` / … sections into one of each in Keep-a-Changelog order, while `CHANGELOG.md` itself stays as merged.

## The announcement

Every release opens with prose describing what happened, before the changeset. A release section is 460 bullets in the large case, each written by whoever merged it and correct on its own; nobody reads that and comes away knowing what the release *is*, and an upgrade note that wants an action from an operator is one bullet among hundreds with nothing marking it as urgent. The announcement is the only place the release is described as a whole.

**It is the `[Unreleased]` section's preamble: everything between the `## [Unreleased]` line and the first `### `.** That location is not arbitrary — both extractors already bucket pre-`###` content under a `None` key and emit it first, so an announcement written there reaches the tag annotation and the GitHub Release body with no change to either, and reaches anyone reading `CHANGELOG.md` in the repository at the same time. One text, four surfaces.

**No headings inside it.** A `### Highlights` is read by both extractors as a changeset subsection, bucketed, and emitted *after* `### Security` — the announcement would arrive at the bottom of the release notes it is meant to open. A `#` or `##` outranks the version heading it sits under. Paragraphs and bold lead-ins only. `scripts/release.sh` refuses a cut with either mistake in it, and refuses one with no announcement at all; `--no-announcement` is the escape for a hotfix that genuinely needs none (a single-bullet patch release), not a way past a cut you have not written yet.

**Write it last, against the finished section.** Bullets accumulate per merge over weeks from branches that never see each other; the announcement is written once, immediately before running `release.sh`, by someone who has just read the whole of `[Unreleased]`. Written earlier it describes a release that then kept changing.

## Writing one

Read the entire `[Unreleased]` section first. Not the headings, not the first sentence of each bullet — the whole thing. The themes are not the Keep-a-Changelog buckets and cannot be derived from them: in 0.41 the largest single theme was spread across `Security`, `Fixed` and `Changed`, and the `Added` bullets for offline web chat, the notification bell and usage reporting are three separate features that arrived interleaved.

Then, in order:

1. **A lede that measures the release.** Commit count against the two previous releases (`git log --oneline --no-merges vPREV..HEAD | wc -l`) and the one or two things that account for most of it. A number a reader can check beats an adjective.
2. **One paragraph per theme**, four to eight of them, ordered by how many people it reaches — what changed, what it was before, and what a person now sees. Group across buckets. A theme carrying real weight gets its own paragraph even where the changeset spread it over thirty bullets.
3. **`**Before you upgrade.**` last**, as a list. One entry per thing that wants a decision or an action: a credential to reissue or rotate, a setting that is gone or has changed meaning, a migration a deploy performs and its refusal conditions, a default that flipped, a new alert to expect. Say what to do, not only what changed. Every `**Upgrade note:**` bullet in the section is a candidate; a bullet that merely says something is fixed is not, however large the fix.

What to leave out: anything with no reader outside this repository (a refactor, a test-only change, a rule file), and the enumeration itself — the changeset is directly below and the announcement does not summarise all of it. An entry that matters and did not make a paragraph is not lost; it is one screen down.

Voice is the changelog's own: second person, plain, specific, what it does rather than what it enables. Bold is for the `Before you upgrade` lead-ins and nothing else. The full checklist is the `writing-style` skill, which is worth invoking before drafting rather than after.

Length tracks the release. 873 commits earned eleven paragraphs and a twelve-item upgrade list — about 1,250 words of prose and 550 of list; 36 commits earn three sentences and no list at all. The worked example is the announcement opening the most recent release in `CHANGELOG.md` (0.41's, written under `[Unreleased]` before its cut) — read it beside the section it opens.

## Two gotchas that have already cost a release

- **A tag force-push does not re-trigger `push: tags:`.** A workflow that ran with bad input cannot be fixed by re-pushing the tag; re-run it through `workflow_dispatch` with the tag as input.
- **`git tag -a -m` strips lines starting with `#`** unless `--cleanup=verbatim` is passed, which `release.sh` does. Anything else building an annotation from the changeset needs it too.
