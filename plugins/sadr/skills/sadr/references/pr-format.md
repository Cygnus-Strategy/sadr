# The Sadr pull request format

Every Sadr PR uses this format exactly. The same title shape, the same sections, and the same order make every Sadr PR easy to recognize and easy to read. `report.py` also reads these PRs, so a change to the format breaks the reports.

A repo's AGENTS.md can add a section, and its conventions bind Sadr where they are compatible, for example draft PRs, labels and assignees. It cannot remove, rename or reorder a section, because the reports depend on the names. See "Precedence" in SKILL.md and references/agents-md.md.

Write every PR body with the rules in references/writing.md.

## Kinds of PR

| Kind | Branch | Title | Holds |
|---|---|---|---|
| Facts | `sadr/facts` | `sadr: AGENTS.md facts YYYY-MM-DD` | every AGENTS.md change of the run, including any `### Holds` entry that Sadr proposes |
| Upkeep | `sadr/upkeep` | `sadr: upkeep YYYY-MM-DD` | dependency, runtime and pin upgrades, scanner fixes, and the cooldown caps and holds for them in the package manager config. Never AGENTS.md. |
| Issue | `sadr/issue-<N>` | `sadr: #<N> <issue title>` | one issue |
| Issue, one part | `sadr/issue-<N>-<part>` | `sadr: #<N> <issue title> (<part>)` | one part of a multi-part issue |

- A run with nothing to do in a repo opens no PR and posts no comment. The run report lists the repo as "nothing to do".
- Each kind is its own branch off the default branch. No PR depends on another, so a maintainer can merge them in any order.
- Only the facts PR changes AGENTS.md.
- A repo has at most one open facts PR and one open upkeep PR. A later run adds commits to it and rewrites its body. The date in the title is the date of the latest run.
- A repo has at most one open issue PR for each issue, or for each part of a multi-part issue.
- Never force-push a Sadr branch.

## Sections, in this order

Write every section in every PR. When a section has nothing to report, write `- None.` under it. Do not remove a section.

```
## What this PR does
<Two or three sentences. Say what changed and why. Plain level.>

## Changes
- <one line for each change: "fastapi 0.143 → 0.148", "Python 3.14 → 3.15 (.python-version, pyproject.toml, Dockerfile, CI)">
- <a major upgrade gets the migration in parentheses>

## Supply chain
- cooldown: <the setting for each package manager, or "unchanged">
- pins: <actions or images that Sadr pinned, or "unchanged">
- cooldown exceptions: <package version, advisory ids, capped at, expires UTC timestamp, or "none">
- inside the cooldown (already merged): <package version, capped until UTC timestamp when Sadr added a cap, or "none">
- waiting for cooldown: <package version (clears UTC timestamp), or "none">

## Security findings
- untriaged: <before> → <after> (<sources: "Semgrep API and semgrep ci --dry-run">)
- open: <before> → <after>
- fixed: <count> (<fix>: <ids> | <fix>: <ids>)
- false positive: <count> (<id>: <the reason>)
- accepted risk: <count> (<id>: <who is exposed and why that is acceptable>)
- other: <count> (<id>: <state>)
- suppressions reviewed: <count>

## Holds
- <added | lifted | kept>: <each cooldown cap or Sadr hold in the package manager config, with its file and the UTC time it clears or its blocker>
- honored: <each ### Holds entry or Settings line that kept Sadr from a change>

## AGENTS.md
- <in a facts PR, each fact that Sadr wrote into the ## Sadr section. In another PR, "None." or a link to the facts PR>

## Merging
- auto-merge: <the level for this kind of PR> (<repo Settings, owner Settings in OWNER/.github, or Sadr default, from setting_source in merge_guard.py>, <kind> PRs)

## Needs you
- <each action that a maintainer must take in this repo now, one line each>

## Verification
- <each command that Sadr ran, and its result: "uv run pytest: 142 passed">

## Run
- <minutes> minutes, US$<spend> (or "spend not reported")

## Not done or not checked
- <each check that could not run on the host, with what and why, and any risk the reviewer must know>

---
Opened by [Sadr](https://sadr.cygnusstrategy.com), a dark factory for software.
```

The footer is always the last two lines, with the link, exactly as above.

## Line breaks

Do not wrap lines at a fixed width. Write each paragraph as one line, and each bullet as one line, however long. The reader's client wraps the text to the width of the screen. A line break inside a paragraph or a bullet makes ragged lines on GitHub and in other Markdown readers. Put a line break only between paragraphs, between bullets, and inside code blocks.

## Rules for the sections

- **What this PR does** for an issue PR also says `Closes #<N>.` For one part of a multi-part issue, it says `Part of #<N>.` instead, and names the part, so that the merge does not close the issue.
- **What this PR does** for a facts PR says in one or two sentences what Sadr had to discover. The facts PR writes `- None.` under each section that does not apply to it, such as `## Supply chain` and `## Security findings`.
- **Changes** has one line for one change. Group the lines by kind: runtimes, dependencies, CI, code. A bump of Sadr's own release pin is a CI line: `Sadr v1.2.0 → v1.3.0 (.github/workflows/sadr.yml)`.
- **Supply chain** keeps the five line shapes above. Sadr never rolls back, so there is no rollback line.
- **Holds** lists what lives in the package manager config, as "Sadr's cooldown caps" and "Holds" in references/policy.md say. For example:
  - `added: cooldown exception certifi 2026.10.19 for GHSA-xxxx-xxxx-xxxx, capped at 2026.10.19, clears 2026-10-27T02:24Z (pyproject.toml)`
  - `added: already merged pillow 13.0.0, capped at 13.0.0, clears 2026-10-23T22:17Z (pyproject.toml)`
  - `lifted: already merged sqlalchemy 2.1.5, cleared 2026-10-23T20:52Z (pyproject.toml)`
  - `kept: hold Python at 3.14 (newest 3.15), Cloudflare Python Workers do not support 3.15 yet (.python-version)`

  It never asks a maintainer to remove a cap or to edit AGENTS.md. A later run lifts each cap. A `### Holds` entry that Sadr proposes goes in the facts PR.
- **Security findings** keeps the exact line shapes above. `report.py` reads the counts from the start of each line.
  - `untriaged` counts findings with no triage decision. `open` counts findings that the default branch's scan still sees, triaged or not.
  - Group `fixed` by the fix that closed them, and name each advisory by its GHSA or CVE id, for example `fixed: 31 (pillow 12.3.0 → 12.4.0: GHSA-xxxx-xxxx-xxxx, CVE-2026-00000 and 29 more | 1035592492 python.flask.security.xss in app.py:12)`.
  - `other` counts findings in a state Sadr did not set, such as `fixing` from an earlier tool or `reviewing`.
- **Needs you** holds only actions that a maintainer must take in this repo now. Each bullet is one action, written so that the maintainer can do it without reading anything else. A retired AI model or another break on the default branch comes first. Write `- None.` when nothing waits on a maintainer. It never holds:
  - what Sadr changed since the last run, or how this PR differs from an earlier version of itself. That goes under `## Changes`.
  - a resolved item. Drop it on the next run.
  - a gap for the whole owner account, such as Semgrep Secrets turned off or an App permission that the install lacks. It goes once in the run report, never in any PR section.
  - a remark about AGENTS.md and other Sadr PRs. Only the facts PR changes AGENTS.md, so there is nothing to resolve.
  - a commit's signature state. `commit.py` reports a commit that GitHub did not verify. That goes in the run report.

  A cooldown exception is one line: `Review the cooldown exception for certifi 2026.10.19 (GHSA-xxxx-xxxx-xxxx). Sadr lifts the cap after 2026-10-27T02:24Z.`
- **Run** is written by the coordinator, not by a worker, from the time and spend that the coordinator measured. In GitHub Actions, the one session does both jobs. When the spend is unknown, write `spend not reported`. Never write a guessed number or `US$0`.
- **Not done or not checked** names each check that Sadr could not run on the host, and says exactly what and why, for example `The iOS UI tests need macOS. The runner is Linux.` It also names a risk that the reviewer must know. It never holds:
  - a check that Sadr could have run. Build the development environment and run it, as "Verification" in references/policy.md says.
  - an expected float, such as an image tag that floats inside its digest pin or the `ubuntu-latest` runner label. List floats only when Settings ask for them.
  - a gap for the whole owner account. It goes in the run report.

  It is never empty on an issue PR. A feature always has an edge that Sadr did not test.
- Do not add a section that is not in this file, unless the repo's AGENTS.md asks for it. Put an added section before `## Run`.
