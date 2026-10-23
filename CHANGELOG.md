# Changelog

Sadr follows [semantic versioning](https://semver.org). The policy in
`plugins/sadr/skills/sadr/references/` changes what happens in every
adopter's repos. So each release lists its policy changes:

- A fix or a clarification is a patch release.
- A new rule or a stricter rule is a minor release.
- A looser rule or a removed rule is a major release.

## v1.0.0 (unreleased)

The first release.

### Added

- The Sadr plugin for Claude Code. The repo is its own plugin marketplace.
- A `model` input on both workflows, `claude-opus-5-5` by default, that picks the Claude model for the run.
- The skill and its run procedure: scope discovery on every run, one worker for each repo, preview mode, time and spend limits, and schedule caps.
- Up to three independent PRs for each repo and run: one for AGENTS.md facts (`sadr/facts`), one for upkeep (`sadr/upkeep`), and one for each enhancement issue (`sadr/issue-<N>`). A repo with nothing to do gets no PR and no comment.
- Building accepted enhancement issues, off by default.
- Auto-merge through GitHub's required checks, with a separate setting for facts, upkeep and issue PRs, all off by default. `repos.py` reads the owner's `OWNER/.github` AGENTS.md once per owner and gives each repo the repo's and the owner's `## Sadr` text. `merge_guard.py` resolves the setting from both and reports whether it came from the repo, the owner or Sadr's default.
- A secrets check with gitleaks before every push.
- The AGENTS.md conventions in `docs/agents-md.md`, with examples.
- The reusable GitHub Actions workflows for one repo and for a whole organization or user account. Both take the Sadr release as a commit SHA, load the plugin at that SHA, and act through a GitHub App.
- A GitHub App as Sadr's identity, which each owner creates from `app/manifest.json` and owns: commits created through `createCommitOnBranch` (the git data API only for merge commits, symlinks, submodules and mode changes), authored by the App's bot user and marked Verified by GitHub, which signs API commits only under the App's own identity, with a warning when it does not verify one, a write token for each repo, a read-only token for `OWNER/.github` and declared related repos, tokens renewed every 45 minutes, and a documented degraded mode with `GITHUB_TOKEN` only.
- `app/create.html`, `app/README.md` and the App logo, with a CI check that the manifest, the logo and the README setup steps match the documented permissions.
- Sadr's upkeep PR moves an adopter's pin of Sadr's own workflow to newer releases, under the cooldown.
- Related repos declared in AGENTS.md, or inferred from a checked-in build file, built against as clean read-only worktrees of their default branch.
- `references/agents-md.md` and `references/permissions.md` for the run, with one format for the holds and maintainer tests that people keep in AGENTS.md, and UTC timestamps.
- Preview mode writes each PR body, with its title on line 1, to a folder unique to the run that `report.py --bodies` reads.

### Policy

- A supply-chain cooldown of 7 days for every package manager. A repo's Settings can raise or lower it. Exceptions only for advisory fixes, capped at the fixed release, and a cooldown never moves a package out of an advisory fix. Sadr never rolls back a merged version. When it adds a cooldown to a lock with younger versions, it caps them until they clear.
- Every lockfile entry dated on every run. Setup actions on exact versions, never below what a floating spec resolves to today. Node on the newest LTS line at the cooldown cutoff.
- Before 1.0, a minor bump counts as major for auto-merge.
- Runtimes, toolchains and dependencies on their newest releases that cleared the cooldown, majors included.
- Actions and reusable workflows pinned to a verified commit SHA, images pinned by digest, and installers pinned.
- Every real Semgrep finding fixed in the PR, never deferred to an issue.
- The build and tests green after each step, without running scripts that call live services.
- Sadr's cooldown caps are stateless: each lives only in the package manager config with a `# sadr:` comment, and every run derives its expiry again and lifts it once it clears. Only the facts PR changes AGENTS.md.
- Sadr's commit subjects start with `sadr: `, unless the repo's AGENTS.md states another convention.
