# Identity and permissions

Sadr acts on GitHub as one identity with the least access that its work needs. This file says who Sadr is on GitHub, which tokens a run gets, how long they last, and what Sadr does when a token cannot reach something. It is one design for every case. Do not patch around a missing permission in one repo. Report it.

## Sadr's identity

Every run acts as the owner's own Sadr GitHub App, the first run included. Each owner creates its own copy of the App from the manifest that Sadr ships in `app/manifest.json`, and installs it on its account. The private key stays in the owner's secrets. Nobody else, Cygnus Strategy included, holds a key to it. The App is only an identity: every rule lives in the plugin and the workflows. The manifest changes only when the permissions change. `app/README.md` in the Sadr repo has the setup steps.

Sadr commits, pushes, opens PRs and comments as the App's bot user. GitHub signs a commit made through the API, and shows it as Verified, only when the commit carries the App's own identity. So every published commit shows the App's bot user as its author, never the name `Sadr`:

- name: `<app-slug>[bot]`, such as `sadrbot[bot]`
- email: `<bot-user-id>+<app-slug>[bot]@users.noreply.github.com`

Each owner's App has its own slug and bot user id. Never hard-code either. Look them up at run time:

```bash
python3 scripts/identity.py --json
```

It takes the App's slug from `SADR_APP_SLUG` or `--app-slug`, looks up the bot user with `gh api /users/<app-slug>%5Bbot%5D`, and prints the name and email. The local commits use this identity, so that a `Signed-off-by:` line matches the published author. The workflows set it before the run starts. Do not change it.

### Signed commits

Build each commit locally with `git commit`, so that the build and the secrets check see the real tree. Then publish the branch through the GitHub API with the App token:

```bash
python3 scripts/commit.py --repo OWNER/NAME --branch BRANCH
```

It recreates each local commit that the remote branch lacks, with the same message and tree, through GitHub's `createCommitOnBranch` GraphQL mutation. GitHub makes the App's bot login, such as `sadrbot[bot]`, the author, signs the commit, and shows it as Verified. The author date is the publish time. A commit that changes `.github/workflows/` needs the Workflows permission on the token. The local commits are only the build. The published commits are the API commits.

`createCommitOnBranch` cannot express a merge commit, a symlink, a submodule, an executable file or a mode change. For those commits only, `commit.py` falls back to the git data API (blobs, a tree, then a commit) and lists the reasons in its result. That commit names no author and no committer, so GitHub uses the App's identity and signs it too. Never pass an explicit author: GitHub then leaves the commit unsigned.

`commit.py` reports whether GitHub verified each commit it creates. For a git data commit, it reads `verification.verified` back. When GitHub does not verify a commit, the script prints a warning and still publishes. Put the unverified commits under "Needs you".

### Local runs and preview

A local run in run mode needs the App too. Export `SADR_APP_TOKEN`, an installation token of the owner's App, and `SADR_READ_TOKEN`. Without them, run mode stops at step 1 with one "Needs you" item in the report. It never pushes as the user's own login.

Preview pushes nothing, so it needs no App. In each worker clone, set `git config user.useConfigOnly true`, so that git never makes up an identity from the machine's hostname. When the clone has no identity, set the placeholder from `scripts/identity.py --placeholder`, `Sadr (preview) <sadr-preview@localhost.invalid>`, in the clone's own config, never globally. Never commit as a hostname address.

When the repo's guidance asks for a sign-off (a DCO check, CONTRIBUTING, or AGENTS.md), add `Signed-off-by:` with the same identity.

## The App's permissions

Grant the App these repository permissions, and nothing else:

| Permission | Access | Why |
|---|---|---|
| Contents | read and write | push `sadr/` branches, read related repos |
| Pull requests | read and write | open and update PRs, turn on auto-merge |
| Issues | read and write | read enhancement issues, comment on them |
| Workflows | read and write | pin actions in `.github/workflows/` |
| Checks | read | read check results |
| Commit statuses | read | read status results |
| Metadata | read | required by GitHub |

Administration (read) is optional. Without it, the API refuses to show classic branch protection, `merge_guard.py` reports `cannot_verify`, and Sadr leaves auto-merge off. Rulesets are readable without it.

Install the App on all repos, or on the repos in scope plus `OWNER/.github` and every repo that another repo declares in `### Related repos`.

## Tokens

An App token is scoped to repos and permissions when it is minted. Each repo's run gets two:

| Environment variable | Repos | Access |
|---|---|---|
| `SADR_APP_TOKEN` | the repo being worked on | the write permissions above |
| `SADR_READ_TOKEN` | `OWNER/.github` and the related repos that the repo's `## Sadr` section declares | contents and metadata, read only |

The org workflow also mints an owner-wide read-only token for discovery and the report. That token never reaches a worker. It reads `OWNER/.github` AGENTS.md for `repos.py`, so `owner_sadr_section` is filled before the session starts.

Use `SADR_APP_TOKEN` for every write to the repo. Use `SADR_READ_TOKEN` only to read the repos it covers. Never use one repo's token for another repo. Never print a token, write it to a file, put it in a remote URL, a commit, a PR body or a comment.

### Token lifetime

An App token expires one hour after it is minted. A run can take six hours. So:

- In GitHub Actions, the workflow mints new tokens every 45 minutes for the whole job. `git` uses them through a credential helper, and `gh` through a wrapper on `PATH`. The `sadr-token write` and `sadr-token read` commands print the current tokens for any other tool. The values in `SADR_APP_TOKEN` and `SADR_READ_TOKEN` are the first tokens. Do not use them after the first hour.
- To renew the tokens, the job holds the App's private key in the memory of a background process. The agent can run commands with sudo on the runner, so a session that something hijacks can reach that key. Sadr accepts this risk so that a run can last longer than one hour. To limit the risk, give the App only the permissions in this file, install it only on the repos Sadr works on, and rotate the key if you suspect that a run was compromised.
- Before each push in GitHub Actions with an App, check that `git remote get-url origin` holds no credentials. The action that starts the session writes its first token into that URL, and that token expires. If the URL holds one, set it to the plain `https://github.com/OWNER/NAME.git` URL, so that the credential helper supplies a fresh token. In the degraded mode, leave the URL alone: `GITHUB_TOKEN` lasts for the whole job.
- In a local run, Sadr cannot renew a token it did not mint. Before each push, check that `SADR_APP_TOKEN` still works with `GH_TOKEN="$SADR_APP_TOKEN" gh api /installation/repositories --silent`. When it has expired, stop the repo cleanly as "Limits" in SKILL.md says, and report it under "Needs you".

### Without an App: the degraded mode

A per-repo workflow can run with the workflow's own `GITHUB_TOKEN` and no App. Then `SADR_APP_TOKEN` and `SADR_READ_TOKEN` are empty, and four things do not work:

- **Workflow files.** GitHub refuses a commit from `GITHUB_TOKEN` that changes `.github/workflows/`. Leave every workflow-file change out of the branch. Under "Needs you", list each change a maintainer must make, with the exact line, for example the pinned `uses:` line.
- **Other repos.** `GITHUB_TOKEN` reads only its own repo. A private `OWNER/.github` or a private related repo is unreachable. GitHub answers 404 for a private repo the token cannot see, so `repos.py` then reports `owner_agents_md` as false. Report each one under "Needs you", and say that the owner's Settings were not read when `OWNER/.github` is private.
- **Push workflows after a merge.** GitHub starts no workflow for an event that `GITHUB_TOKEN` caused. A PR that auto-merge merges does not start the default branch's push workflows, such as a deploy. Say so in `## Merging`.

- **Sadr's identity.** Sadr comments as `github-actions[bot]`. `commit.py` still creates the commits through the API, so `github-actions[bot]` is their author, and reports whether GitHub verified them.

The PR's `## Not done or not checked` says that the run used the degraded mode.

## Related repos: cross-repo builds

A repo that builds against another repo declares it in `### Related repos`, as references/agents-md.md says. Sadr honors the declaration and nothing else:

1. Take the declared repos from `related_repos` in `repos.py`, and the inferred ones from `inferred_related_repos`, as references/agents-md.md says. Compare them with the sentences in the section. A declared repo missing from `related_repos` is unreachable.
2. For each reachable repo, keep a clone under `${XDG_CACHE_HOME:-$HOME/.cache}/sadr/repos/OWNER/NAME` (in GitHub Actions, under `$RUNNER_TEMP`). Fetch it with the read token. Never work in it.
3. Add a detached worktree of its default branch at the declared path. `git worktree add` resolves a relative path against the clone, not against the repo's checkout, so pass an absolute path: `git -C CLONE worktree add --detach "WORKSPACE/NAME/../field-ui" origin/DEFAULT`, where `WORKSPACE` is absolute. The worktree is read-only for Sadr. Never commit in it or push from it.
4. Build against that worktree, never against another worker's branch or a person's clone. So workers can run in any order.
5. Record in the PR's `## Verification` which commit of each related repo the build used.
6. When the repo is done, remove the worktree with `git -C CLONE worktree remove --force PATH`.

The declared path must stay inside the worker's own workspace. A local worker's workspace is `${XDG_CACHE_HOME:-$HOME/.cache}/sadr/work/OWNER/NAME/`, and the repo's checkout is `NAME/` inside it, so `../field-ui` lands in that workspace. In GitHub Actions, the parent folder of the checkout is the workspace. A path that leaves the workspace goes under "Needs you".

When a related repo is unreachable, the build that needs it cannot run. Report under "Needs you" which repo, and what to grant: install the App on it, or make it readable. Under `## Not done or not checked`, say which checks did not run. Never skip the report in silence, and never fall back to a stale copy.

## Pushing

- Create a branch with `git switch --no-track -c BRANCH origin/DEFAULT`, so that it tracks nothing.
- Publish only with `scripts/commit.py --repo OWNER/NAME --branch BRANCH`, as "Signed commits" says. It creates the remote branch when it does not exist. Never run `git push` to publish a Sadr branch.
- Run the secrets check in references/policy.md before every push.
- Never force-push, and never push to a default branch.
