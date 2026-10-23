# Merging

Sadr does not merge by default. A maintainer merges each PR. Settings can turn on auto-merge for each kind of PR, for an owner, and a repo can turn it on or off for itself.

A repo's AGENTS.md and its owner's AGENTS.md win over every rule in this file, within the limits in "Precedence" in SKILL.md. Sadr never pushes to a default branch, whatever the setting.

## The setting

Each kind of PR has its own setting. All three are off by default.

| Kind | Levels | What the level covers |
|---|---|---|
| Facts PR (`sadr/facts`) | `off`, `on` | `on`: every facts PR |
| Upkeep PR (`sadr/upkeep`) | `off`, `patch`, `minor`, `all` | `patch`: only patch changes. `minor`: patch and minor changes, as below. `all`: every upkeep PR. |
| Issue PR (`sadr/issue-<N>`) | `off`, `on` | `on`: every PR for an enhancement issue |

Resolve each setting in precedence order: the repo's `### Settings`, then the owner's, then `off`. Read Settings from the default branch only. Settings are plain sentences, for example:

| Sentence | Facts | Upkeep | Issue |
|---|---|---|---|
| "Auto-merge facts and upkeep PRs. Enhancement PRs need review." | on | all | off |
| "Auto-merge upkeep PRs with minor updates." | off | minor | off |
| "Auto-merge facts PRs and patch updates." | on | patch | off |
| "Auto-merge everything." | on | all | on |
| "Don't auto-merge." | off | off | off |

"Housekeeping" means upkeep. "Feature" and "enhancement" mean issue. A repo sentence about one kind leaves the owner's setting for the other kinds in place. When a sentence is unclear, use `off` for the kinds it may mean, and say so under "Needs you".

### What counts as patch and minor

An upkeep PR is `patch` when every change in it is one of these:

- a patch version change of a dependency, an action or an image
- a cooldown setting, a SHA pin or a digest pin with no version change
- a fix for a scanner finding that changes no version past patch
- a cooldown cap that Sadr adds or lifts in the package manager config, with its `# sadr:` comment. A version that a lift moves counts as its own change.

An upkeep PR is `minor` when every change in it is `patch` or a minor version change of a dependency, an action or an image. A patch change of an exact runtime pin, such as `node-version` from `24.11.0` to `24.11.1`, is minor too.

An upkeep PR is neither when it has any of these:

- a major version change of any dependency, action or image, Sadr's own release included
- a runtime or toolchain change to a new line, such as Node 24 to 26, or Python 3.14 to 3.15

Before 1.0, a minor bump counts as major. For a `0.x` version, a change of the first non-zero number is major: 0.143 to 0.148 is major, and 0.143.1 to 0.143.2 is a patch. For `0.0.x`, every change is major.

## Guards

The guards apply to every kind of PR. Check them only when the PR's setting is not `off`. Under `off`, Sadr turns nothing on, and none of these is a "Needs you" item.

Read the repo's merge settings with:

```bash
python3 scripts/merge_guard.py --repo OWNER/NAME --kind facts|upkeep|issue --json
```

It prints whether the repo allows auto-merge, the allowed merge methods, the number of required status checks (or `cannot_verify` with the reason), and `eligible`.

Sadr leaves the PR for a maintainer when any of these is true:

- The PR has an item under "Needs you" about the PR's own change. A cooldown exception and an unverified build target are always one. A gap for the whole account, such as Semgrep Secrets turned off, goes once in the run report and never blocks auto-merge.
- The build or the tests failed in the run.
- `eligible` is false: the repo does not allow auto-merge, or the default branch has no ruleset or branch protection that requires a status check.
- `merge_guard.py` reports `cannot_verify`. GitHub refuses to show branch protection on a private repo of a free plan (HTTP 403), and without the App's Administration permission. Sadr cannot prove that a check guards the merge, so it does not turn auto-merge on.

For each guard that blocks auto-merge, add an item under "Needs you" that says what to turn on, or that Sadr could not verify the protection and why.

## How Sadr turns on auto-merge

```bash
gh pr merge NUMBER -R OWNER/NAME --auto --MERGE_METHOD
```

GitHub then merges the PR only after every required check passes. Sadr never merges a PR at once, and never uses `--admin` to skip a check.

Pick the merge method from the allowed methods that `merge_guard.py` prints:

1. The method that Settings name, when the repo allows it.
2. Otherwise the only method the repo allows.
3. Otherwise the first allowed method in the order of GitHub's merge button: `--merge`, then `--squash`, then `--rebase`.

When a later run adds a "Needs you" item to a PR that has auto-merge on, turn auto-merge off with `gh pr merge NUMBER -R OWNER/NAME --disable-auto`.

In preview mode, turn on nothing. Say in the PR body what Sadr would do.

## The token and push workflows

GitHub starts no workflow for an event that a workflow's own `GITHUB_TOKEN` caused. Sadr turns on auto-merge with its App token, `SADR_APP_TOKEN`, as references/permissions.md says. A merge that auto-merge makes then starts the workflows that run on push to the default branch, such as a deploy.

In the degraded mode without an App, Sadr uses `GITHUB_TOKEN`, and a merge does not start push workflows. Say so in `## Merging`.

## The PR body

Every PR body says which setting applied, where it came from, and what Sadr did, in its `## Merging` section. For example:

```
## Merging
- auto-merge: off (Sadr default)
```

```
## Merging
- auto-merge: minor (repo Settings, upkeep PRs)
- turned on auto-merge (squash)
```

```
## Merging
- auto-merge: minor (owner Settings, upkeep PRs)
- left for a maintainer: pydantic 2 → 3 is a major upgrade
```

```
## Merging
- auto-merge: on (owner Settings, facts PRs)
- turned on auto-merge (merge)
```
