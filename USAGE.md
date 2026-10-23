# Using Sadr

This page is for after the [install](README.md#install). It covers how to run Sadr, how to tell it about your repos, how to hand it issues, and what comes back. [docs/agents-md.md](docs/agents-md.md) is the full reference for the AGENTS.md conventions.

## Run it

**From Claude Code.** Ask in plain language. Start with a preview, which does the whole run in local clones but writes nothing to GitHub:

```text
Preview Sadr on the acme organization.
Run Sadr on acme/api and acme/web.
```

**From GitHub Actions.** The workflow from the install runs on its schedule. Run it by hand from the Actions tab, or with `gh workflow run`. The reusable workflows take these inputs:

| Input | Default | What it does |
|---|---|---|
| `preview` | `false` | Do the whole run, push nothing, and write each result to the job summary. |
| `model` | `claude-opus-5-5` | The Claude model that runs Sadr. |
| `max-spend-usd` | `200` | The most one repo's run may spend, with API billing. |
| `max-parallel` | `4` | The most repos that run at the same time (organization workflow). |
| `owner` | the calling repo's owner | The organization or user account to run on (organization workflow). |
| `repos` | every repo | Only these repos of the owner, by name (organization workflow). |

A repo that got a push to a `sadr/` branch in the last hour is skipped, in case another run is still working on it.

## Tell it about your repos

Sadr reads the `## Sadr` section of each repo's AGENTS.md, in plain sentences. An organization or user account writes its defaults the same way, in the AGENTS.md of its `.github` repo. The repo's own section wins.

```markdown
## Sadr

### Build and test
- `uv sync --locked && uv run pytest`

### Settings
- Auto-merge facts and upkeep PRs. Enhancement PRs need review.
```

Sadr fills in what it learns, such as build steps and toolchain quirks, through its facts PR. You write `### Settings` and `### Don't touch` yourself. [docs/agents-md.md](docs/agents-md.md) lists every subsection and every setting Sadr understands.

To keep a repo out of Sadr's runs, put this line in its `## Sadr` section:

```markdown
Sadr: off. The reason, for whoever reads this next.
```

Archived repos, forks and empty repos are skipped without it.

## Hand it issues

A maintainer accepts an issue by applying the `sadr` label. Only a label applied by a user with write access counts, so anyone can file an idea, but only a maintainer can send it to Sadr. Create the label once in each repo, under Issues, then Labels, or with `gh label create sadr`. No label, no work.

- **One issue per run** by default, oldest first. "Build at most two issues per run." in the repo's AGENTS.md raises it.
- **Another label.** "Build issues labeled `ready`." uses your own label instead.
- **Off.** "Don't build issues." turns it off.

For each accepted issue, Sadr does one of three things: it builds it in a PR that closes the issue, it comments with the questions that block the work, or it comments that merged PRs already deliver it. An issue that plans several PRs gets one part per run, each marked `Part of #N`. Sadr waits while its own question has no answer, and while a maintainer's request for another maintainer's sign-off has no reply. [docs/agents-md.md](docs/agents-md.md#issues) has the details.

## What comes back

Each run leaves at most three kinds of pull request in a repo, each on its own branch:

- **Facts** (`sadr/facts`): what Sadr learned about the repo, written into its AGENTS.md.
- **Upkeep** (`sadr/upkeep`): dependency, runtime and CI upgrades, pins, and fixes for security findings.
- **Issue** (`sadr/issue-<N>`): one per issue it built.

A repo that already meets the standard gets no PR and no comment. Every PR has the same sections in the same order. Read `## Needs you` first: it holds only what a maintainer must do in that repo now. The run report lists anything that affects the whole account once, such as a scanner product that is turned off.

When the repo asks for draft PRs, Sadr opens drafts and marks each one ready once its checks pass.

## Let it merge

Sadr merges nothing by default. Turn on auto-merge for each kind of PR with a sentence in `### Settings`:

| Sentence | Facts | Upkeep | Issue |
|---|---|---|---|
| "Auto-merge facts and upkeep PRs. Enhancement PRs need review." | on | all | off |
| "Auto-merge upkeep PRs with minor updates." | off | minor | off |
| "Auto-merge everything." | on | all | on |
| "Don't auto-merge." | off | off | off |

Sadr turns auto-merge on only when the repo allows it and the default branch requires at least one status check, through a ruleset or branch protection. So nothing merges untested. A PR that skips the cooldown to fix a security advisory always waits for a person.

## Limits

Each repo's run stops cleanly at 6 hours or US$200 by default, with what it finished pushed and the rest named in the PR. A repo's or owner's Settings can lower either limit, for example "Run limits: 4 hours and $100 per repo."
