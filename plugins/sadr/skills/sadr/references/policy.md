# The Sadr standard

This file says what one run brings each repo to, and the rules for getting there. It applies to every ecosystem. Sadr keeps no list of supported package managers. The worker reads the repo the way an engineer would, finds every ecosystem in it, and applies the standard with that ecosystem's own tools and documentation.

The examples in this file name real tools: npm, uv, Go, Swift, Cargo and others. They are examples, not an allow-list. A repo that uses Gradle, Maven, Composer, .NET, Terraform, Helm, Nix, Docker, ESP-IDF or anything else gets the same standard. The section "Known traps" at the end grows over time.

A repo's AGENTS.md and its owner's AGENTS.md win over every rule in this file, within the limits in "Precedence" in SKILL.md. A repo's own case, such as a board it targets or a tag it tracks on purpose, belongs in its AGENTS.md, not in this file.

## What "newest" means

Throughout this file, "newest" means the newest stable release that has been public for at least the cooldown age. The default cooldown is 7 days. Settings can set another value, overall or for one ecosystem, higher or lower. The repo's value wins. Without a setting, Sadr never lowers a cooldown that a repo already has.

The **cooldown cutoff** is the time of the run minus the cooldown age. A release published before the cutoff is old enough.

Stable means no prerelease, release candidate, beta, nightly or draft. Two cases are allowed:

- A repo that depends on a prerelease line on purpose stays on that line. Its AGENTS.md says so, or the worker asks under "Needs you".
- A transitive prerelease that a stable parent pins exactly stays. Sadr cannot change it without changing the parent.

## The order of work

Work in this order. Commit each step on its own, so that a reviewer can read it and a failure is easy to find and revert.

1. The supply-chain cooldown.
2. Runtimes and toolchains.
3. Dependencies.
4. CI, images and installers.
5. Scanner findings. See references/scanning.md.
6. Cooldown caps and holds.

Before step 1, read the open PRs of other automation in `other_automation_prs` from `repos.py`, such as Dependabot or Renovate. The rule for them is narrow:

- Skip only the files that such a PR changes, and only for dependency and runtime upgrades.
- Never skip the cooldown setting or a fix for a published advisory because of another PR. Do them, and say under "Needs you" that the other PR conflicts.
- List each such PR once under "Not done or not checked", with the files Sadr skipped. Do not list the skipped packages one by one.

## Build and test

### Finding the commands

Use the first source that answers:

1. `### Build and test` in the repo's `## Sadr` section.
2. The rest of the repo's AGENTS.md, CLAUDE.md, README and contributing guide.
3. The test, lint and type-check steps of the repo's CI workflows.
4. The ecosystem's usual command, for example `uv run pytest`, `npm test`, `go test ./...`, `cargo test`, `swift test` or `./gradlew check`.

When a repo has no tests, the check is that it builds, installs and loads cleanly. Say so in the PR. `pytest` exits with code 5 when it collects no tests. That means "no tests", not a failure. Write the command that worked into `### Build and test`, so that the next run does not search again.

### Verification

Verification proves that a change does not break the project before the PR opens. It is not a second CI, so it need not repeat every leg of a CI matrix. Do this for each repo:

1. Build the repo's own development environment the way the repo defines it: devenv or Nix, Docker or compose, a Makefile, package scripts, or whatever AGENTS.md, the README and the CI workflows use. Install the tools it needs on the host, such as Nix and devenv, or Docker, at a release at least the cooldown age old.
2. Inside that environment, run the test suites, linters and type checks that AGENTS.md and the CI workflows run.
3. When people exercise the repo by running the app, run it once: start it against local services or compose, request a health route or the main route, check the answer, and stop it.

Some commands stay off limits, whatever the repo calls them:

- a command that deploys, publishes or releases
- a command that writes to production or to shared remote data
- a command that sends a message, such as an email, a chat post or a notification
- a command that needs production credentials

A script that only reads a public API is fine. Read a script's argument handling before you run it: a `--help` can start its default command. When the repo's own development scripts call a service that is off limits, start the local stand-in that the repo uses, such as a compose service, or skip that one call.

When something cannot run on the host, such as a macOS-only build on Linux or a test that needs hardware, say under "Not done or not checked" exactly what did not run and why.

### Run only what a change can affect

After each step, build and test what the changed files can affect, and skip the rest. A lockfile change in `worker/` does not need the iOS app's UI tests. Under `## Verification`, list each check Sadr skipped and why. Run the whole suite once before the PR when it fits in the time limit.

### Long and exclusive builds

Some builds cannot run many at once on one machine. Xcode is the common case. Take a machine-local lock before such a build:

```bash
locks="${XDG_CACHE_HOME:-$HOME/.cache}/sadr/locks"; mkdir -p "$locks"; slot=1
until mkdir "$locks/xcode.$slot" 2>/dev/null; do slot=$((slot % 3 + 1)); sleep 10; done
# ... build ...
rmdir "$locks/xcode.$slot"
```

`mkdir` is atomic on every platform. Three slots allow three Xcode builds at a time. Remove a lock that is older than the time limit, because its worker is gone. Give each repo its own `-derivedDataPath` under the worker's workspace, and pick a simulator by its UDID, not its name. Run a build that can outlast the shell tool's timeout in the background, and wait for it.

## 1. The supply-chain cooldown

Nothing is installed or pinned until it has been public for the cooldown age. Most malicious releases, such as a hijacked maintainer account, a typosquat or a poisoned tag, are found and removed within days. A week's wait keeps them out.

The repo enforces the cooldown itself, so that installs between runs are covered too:

- **A package manager with a cooldown setting.** Turn the setting on at the cooldown age. Where the repo already declares a minimum version of the tool (`packageManager`, `engines`, the CI setup step), raise it to a version that honors the setting. Do not add a new declaration only for this. `engines` only warns unless `engine-strict` is on.
- **A package manager without one.** Date every lockfile entry, as below, and pin back what Sadr's change made younger than the cooldown age.
- **A dependency bot.** Give Dependabot, Renovate or any other bot the same cooldown on every entry it updates.

Check each setting's name and unit in the installed tool's documentation. The units differ between tools. Some count days, some minutes, some seconds.

### The lockfile is the gate

Some tools refuse a locked version inside the window. Others install it without complaint. So date every entry, every run, not only the entries that changed:

```bash
python3 scripts/lockage.py --min-age-days N PATH...
```

`lockage.py` dates each entry in npm, pnpm, Yarn, Bun and Deno lockfiles, `uv.lock` and uv script locks, `poetry.lock`, `go.sum`, `Cargo.lock` and `Package.resolved`. It lists the entries younger than N days and the entries it cannot date. For another lockfile, date the entries with the registry's API, as "Known traps" shows. List each entry nobody could date under "Not done or not checked".

### What Sadr picks, and what is already merged

The cooldown governs what Sadr picks. Each version that Sadr adds or changes is at least the cooldown age old.

A version that is already on the default branch stays, even when it is inside the window. Sadr does not roll it back only for its age. List it under `## Supply chain` as `- inside the cooldown (already merged): <package> <version>`. A later run sees it age out.

Sadr never rolls a merged version back, and no setting changes that. When a version inside the window was vulnerable, the response belongs to the repo's owner, not to Sadr.

When the newest release clears the cooldown soon after the run, list it as `- waiting for cooldown: <package> <version> (clears <UTC timestamp>)`. A later run takes it.

Lower a version floor in a manifest only when it points inside the window and Sadr's own change put it there. Never lower a floor that a cooldown exception covers.

### Adding a cooldown to a lock that is already young

A new cooldown setting can make the package manager move locked versions back on the next lock. Date the lock with `lockage.py` before you add the setting. For each entry inside the window:

- Add a per-package exemption capped at the version that is locked today, with the tool's exemption syntax from "Exceptions".
- Put the comment `# sadr: already merged inside the cooldown` on the line above the cap, where the format allows comments.
- List it under `- inside the cooldown (already merged):` and under `## Holds` in the PR, with the time it clears the cooldown: its publish time plus the cooldown, as a UTC timestamp.

The cooldown then lands without moving any version. "Sadr's cooldown caps" says when Sadr lifts each cap.

### Pin the tools that install tools

A setup action without an exact version, a floating `npx package@major`, a `curl | sh` installer or a `latest` image installs the newest release, outside every cooldown. Pin each one to an exact version at least the cooldown age old.

A tool that CI runs with `npx`, `uvx` or `pipx` also resolves its own dependencies at run time. Pin it so that those are cooled too:

- npm tools: add the tool as a dev dependency in a `package.json` with a lockfile and the cooldown setting, and run it with `npm exec --no-install` or `npx --no-install`. When an outside service would see the new `package.json` and change what it builds, for example a static site host that runs `npm install` when it finds one, do not add it. Put the choice under "Needs you".
- Python tools: `uvx --exclude-newer "<N> days" tool==X.Y.Z`. The flag cools the tool's dependencies too.
- `pipx`: move to `uvx` as above.

A script that declares its dependencies only in prose, such as `uv run --with`, or a docstring that says "needs X", gets inline script metadata (PEP 723) and a lock, as "Known traps" shows.

### Verify what the deploy installs

A lockfile is a pin only when the deploy installs from it. Read the deploy: the CI deploy job, the Dockerfile, or the service in `### Deploys outside CI`. When the deploy resolves again from the manifest and ignores the lockfile, the cooldown does not reach production. Pin what the deploy reads, for example exact versions in the manifest, or put under "Needs you" what a maintainer must change in the service. Record what the deploy actually installs in `### Deploys outside CI`.

### Structural exemptions

A registry without upload dates cannot be cooled. The repo declares such a case in `### Toolchain`, as references/agents-md.md says. Honor the declaration, leave the cooldown setting off for those paths, and list the exemption under "Not done or not checked". A structural exemption is not a security exception and has no expiry. Never add one on your own judgment. Put the line you would add under "Needs you".

### Exceptions

A release younger than the cooldown age goes in only as the fix for a published advisory that affects the repo, such as a Semgrep supply-chain finding, a GitHub security advisory or an OSV entry. A runtime's security release qualifies in the same way.

The cooldown never moves a package out of a release that fixes a published advisory, whatever the reachability. Before a commit lowers any version, check the lower version against OSV (`https://api.osv.dev/v1/query`) or a Semgrep dry run. When it reintroduces an advisory, keep the fixed release and add an exception instead. Do this in the same commit, so that no commit in the branch downgrades into an advisory.

For each exception:

- Use the tool's exemption for one package. Never lower the global setting.
- Cap the exemption at the fixed release. An exemption without a cap admits every future release of that package.
- Put the comment `# sadr: cooldown exception GHSA-xxxx-xxxx-xxxx, lift when VERSION clears the cooldown` on the line above the cap, where the format allows comments. VERSION is the capped release. Name every advisory id.
- List it under `- cooldown exceptions:` and under `## Holds` in the PR, with the advisory ids and the time it clears the cooldown: the release's publish time plus the cooldown, as a UTC timestamp. List it under "Needs you" too, as one line that asks for a review.

### Sadr's cooldown caps

A cooldown exception and a cap on a version already merged are Sadr's own caps. They are self-describing and keep no state outside the package manager config:

- A cap lives only in the package manager config, such as uv's `exclude-newer-package`, npm's `min-release-age-exclude`, pnpm's `minimumReleaseAgeExclude` or Bun's equivalent. Sadr never writes a cap into AGENTS.md.
- Where the format allows comments, a one-line `# sadr:` comment sits on the line above the cap, in the shapes above. A format without comments, such as `package.json`, gets no comment. There, Sadr treats each per-package cooldown exemption as its own, unless Settings or `### Holds` name the package.
- Each run derives every cap's expiry again: the capped version's publish time plus the cooldown. Read the version from the lockfile, and its publish time from `lockage.py` or the registry.
- Lift each cap whose capped version has cleared the cooldown. Also lift a cooldown exception when a newer release that fixes the same advisory has cleared the cooldown. Then lock again, as the tool's trap in "Known traps" says.
- Keep every other cap as it is.

The upkeep PR lists each cap that it added, lifted or kept under `## Holds`, as references/pr-format.md says. A maintainer does not remove a cap. A later run lifts it.

## 2. Runtimes and toolchains

Each language, framework and toolchain moves to the newest stable line that all its dependencies support.

- **Node** moves to the newest LTS line at the cooldown cutoff: the newest major line whose newest release before the cutoff has an `lts` codename in `https://nodejs.org/dist/index.json`. Active LTS and Maintenance LTS both qualify. Current never does. Use the newest release of that line before the cutoff.
- Settings can set another target, for example "stay one Node LTS behind".
- **A floating spec is already merged at what it resolves to today.** When Sadr pins a floating runtime, such as `node-version: 24`, `python-version: 3.x` or `go-version: stable`, never pick a version below what the spec resolves to today. Never pick a version below the newest security release of that line either. When that release is inside the cooldown, it stays as a cooldown exception if it fixes an advisory, and otherwise it is listed under `- inside the cooldown (already merged):`.
- **Check the runtime's own advisories.** Before you pin a runtime version, check it against its advisory source where that is cheap: `govulncheck` for Go, and OSV (`https://api.osv.dev/v1/query`) for others. A runtime release with a known advisory is not the newest usable release.

Every place that pins the version must agree after the change. Look in version files, manifests, lockfiles, Dockerfiles, CI matrices and setup steps, linter and type-checker targets, build tool configs, and documentation that states the version, such as a README's "requires Go 1.25".

**setup-\* actions.** Every `*-version` input of a setup action (`node-version`, `python-version`, `go-version`, `uv`'s `version`, and so on) is an exact version, such as `24.11.1`, at least the cooldown age old. A version file that the action reads (`.node-version`, `.python-version`, `go.mod`'s `toolchain`) counts as the input and is exact too. `pins.py check` flags a range or a bare major.

When a dependency does not support the newest line yet, step back one line at a time until the build is green. Hold it as "Holds" below says, with the blocking dependency and the issue that tracks it.

Never raise an app's minimum operating system or deployment target. That drops users, and it is a product decision. Suggest it under "Needs you" instead. The same applies to platform compatibility dates that change runtime behavior. Move one only when it is more than three months old, never past the newest date that the locked runtime supports, and run the tests after.

## 3. Dependencies

Every direct dependency moves to its newest release, majors included. Then regenerate the lockfile so that transitive dependencies move too.

- Read the changelog or migration guide for every major upgrade. Make the code changes it needs in the same commit. Do not bump and hope.
- Settings can leave majors for a person. Then list each available major under "Needs you" and take only minor and patch releases.
- Replace a deprecated direct dependency with its maintained successor. Deprecated means marked deprecated or yanked in its registry, archived at its source, or without a release in two years when a successor exists. List a deprecated transitive dependency under "Not done or not checked" with the direct dependency that pulls it in.
- Replace a deprecated API that the compiler or linter warns about.
- Every ecosystem that supports a lockfile has one, committed. A lockfile that `.gitignore` excludes goes under "Needs you". Do not change `.gitignore` on your own.
- Leave alone each package that Settings or `### Holds` name. The setting is the record. Sadr writes no entry for it.

**AI models.** A hard-coded model ID for an AI provider counts as a dependency. When the provider has deprecated it, list the file, line, retirement date and suggested replacement under "Needs you". When the retirement date has passed, the default branch is broken: put it first under "Needs you" and leave auto-merge off. Do not change the model. A new model changes app behavior and needs a live test.

## 4. CI, images and installers

**Actions and reusable workflows.** Each one moves to its latest major, at the newest release at least the cooldown age old. Read the release notes for changed inputs. Pin it to the full commit SHA of that release, with the exact version as a comment:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

A tag can move to other code after the fact. A SHA cannot. The cooldown covers a release that was malicious from the start.

- Get the version and SHA with `scripts/pins.py resolve OWNER/ACTION --min-age-days N`. Never copy a SHA from a PR, an issue or a README. A commit in a fork of the action resolves under the original repo's URL too.
- The comment is the full `vX.Y.Z`, not `v7`.
- Check every pinned file with `scripts/pins.py check FILE...` before the PR: workflows, action files, Dockerfiles and compose files. Fix each problem it lists. Read what it says it skipped, and check those by hand.
- When the newest release is younger than the cooldown age, take the one before it. That needs no hold. A later run takes it.
- Local actions (`./path`) need no pin.

**Images.** Pin each container image by digest, with its tag as a comment, in Dockerfiles, compose files, Kubernetes and Helm manifests, CI `container:`, `services:` and `image:` keys, and `docker://` actions. Many tags are rebuilt often, so the age that counts is the digest's, not the tag's. Get a digest at least the cooldown age old with:

```bash
python3 scripts/pins.py image resolve IMAGE:TAG --min-age-days N
```

Move a base image to the newest tag of the same variant, for example the same `-slim` or `-alpine` variant, or the newest stable distribution codename. Sadr moves digests itself on each run. It adds no bot for that.

**Submodules and cloned repos.** A git submodule, and a third-party repo that a script or Dockerfile clones, moves to the newest release tag at least the cooldown age old. Check that the tag exists on the canonical repo. Pin it by the tag's commit, with the tag in a comment, never by a branch.

**Downloaded build inputs.** A file that the build downloads, such as `cacert.pem` or a model file, comes from a versioned URL with a checksum that the build checks.

**Installers.** Pin every tool that CI or a build script installs, as section 1 says. A script that installs software on users' devices is product code. Do not change what it installs. Note it under "Not done or not checked", and under "Needs you" when it installs a floating version. A floating tag that `### Don't touch` keeps on purpose stays floating.

## 5. Scanner findings

See references/scanning.md. Every real finding is fixed in the run's PR, however large the fix. A finding is never deferred to an issue.

## The secrets check

Run a secrets check before every push, on the commits that the push adds:

```bash
gitleaks git --no-banner --redact --log-opts="origin/DEFAULT..HEAD"
```

Get `gitleaks` from the first source that works:

1. A `gitleaks` on `PATH`.
2. A gitleaks release cached at `${XDG_CACHE_HOME:-$HOME/.cache}/sadr/tools/gitleaks/<version>/gitleaks`. To fill the cache, pick the newest release at least the cooldown age old with `scripts/pins.py resolve gitleaks/gitleaks --min-age-days N`. Download its archive for this platform and its `checksums.txt` from the GitHub release. Check the archive with `shasum -a 256 -c` against the checksums file before you unpack it. Never run a binary that failed the check.
3. The `zricethezav/gitleaks` container image, pinned by a digest from `pins.py image resolve`, with the repo mounted read-only.

When none of these works, push nothing. Report under "Needs you" that the secrets check could not run, and leave the work committed in the local clone.

On a hit, do not push. Rewrite the unpushed commits without the secret, run the check again, and report the finding under "Needs you" without its value.

Once per repo per run, also scan the whole history of the default branch: `gitleaks git --no-banner --redact`. A secret already on the default branch needs rotation, which Sadr cannot do. Report it under "Needs you" without its value. Removing it from the code does not remove it from history. It does not block the push.

## Green

The repo's build and tests pass after each step.

- Never disable, skip or delete a test to make the build green.
- Never loosen a linter, type checker or compiler setting to make the build green.
- A change adds no new test or compiler warning. Fix a new deprecation warning in the same step.
- When a change cannot be made green, step it back to the newest version that is green, and hold it as "Holds" says.
- Build and run the project as "Verification" says. Never run a command that it puts off limits, unless `### Build and test` names it as safe.
- When the default branch was already red before Sadr changed anything, say so under "Needs you" with the failing command. Do not count that failure against Sadr's changes.
- When the host cannot run a check, such as a cross-compiler for another architecture, a release-only platform build or a test on hardware, run the strongest check the host can: compile for the targets it can, or run the unit tests without the device. Under "Not done or not checked", list each target Sadr did not verify, and why. Green then means green for what ran, and auto-merge stays off.

## Holds

A hold records why something stays behind, so that the next run does not retry it blindly. There are two kinds, and the upkeep PR never touches AGENTS.md for either:

- **Sadr's holds.** A version that Sadr stepped back to stay green is pinned in the ecosystem's own config, so that an upgrade tool does not undo it. Where the format allows comments, the line above the pin says `# sadr: hold at VERSION, <the blocker and its issue link>`. Each run checks the blocker again and retries the upgrade when the blocker is gone. The PR lists the hold under `## Holds`. The cooldown caps follow "Sadr's cooldown caps" above.
- **Holds people keep.** `### Holds` in the repo's `## Sadr` section, in the format in references/agents-md.md, and the packages that Settings leave alone. Sadr honors each one. It proposes a new entry, such as a `Maintainer test`, or the removal of a `Hold` whose blocker is gone, only in the facts PR. When an entry's revisit time has passed, Sadr lists it under "Needs you" in the upkeep PR.

Report a hold that is more than 90 days old under "Needs you".

## What Sadr never does

- Push to a default branch, force-push, rewrite a pushed commit, or merge outside references/merging.md.
- Lower or remove a cooldown, unless Settings set a lower value.
- Roll back a version that is already on the default branch.
- Change app behavior, copy, deployment targets or entitlements.
- Commit a secret, a token or an `.env` file.
- Install or pin a release younger than the cooldown age, outside the exceptions above.
- Work in a person's own clone of a repo.

## Known traps

These notes are examples from earlier runs. They do not limit what Sadr handles. Check each one against the tool's current documentation, because tools change.

**JavaScript**

- npm 11.10 and later reads `min-release-age=7` (days) from `.npmrc`. But `npm install` and `npm ci` install lockfile entries that are younger than the window without complaint. Delete and regenerate the lockfile, then date the entries.
- pnpm 10.16 and later reads `minimumReleaseAge` in minutes (10080 is 7 days). Yarn 4.10 and later reads `npmMinimalAgeGate` in minutes. Bun 1.3 and later reads `minimumReleaseAge` in seconds (604800 is 7 days).
- The per-package exemptions are npm `min-release-age-exclude`, pnpm `minimumReleaseAgeExclude` and Yarn `npmPreapprovedPackages`.
- To find outdated packages, use `npm outdated` (or the package manager's own command), which honors `.npmrc`. `npm-check-updates` ignores the `.npmrc` cooldown. If you use it, pin its version and pass `--cooldown N`.
- Deno reads the cooldown from `deno.json`. Semgrep does not parse `deno.lock`, so `lockage.py` is the only check.

**Python**

- uv reads `exclude-newer = "7 days"` in `[tool.uv]` or `uv.toml`. Use the relative form. An absolute timestamp freezes the repo. Check that `uv sync --locked` still passes in CI after you add it.
- uv's window rolls to the minute, not to the calendar day. A release from the afternoon of day 7 can still be refused.
- uv caps an exemption with `exclude-newer-package`, at a time just after the capped release was published. `= false` admits every future release. Follow a change with `uv lock --upgrade-package NAME`. Write the caps as a table, so that each one can carry its `# sadr:` comment:

  ```toml
  [tool.uv.exclude-newer-package]
  # sadr: cooldown exception GHSA-xxxx-xxxx-xxxx, lift when certifi 2026.10.20 clears the cooldown
  certifi = "2026-10-20T03:00:00Z"
  # sadr: already merged inside the cooldown
  pillow = "2026-10-16T22:19:00Z"
  ```
- A standalone script gets PEP 723 inline metadata: `requires-python`, its dependencies, and `[tool.uv] exclude-newer = "7 days"`. Lock it with `uv lock --script FILE` and commit the `FILE.lock` next to it. Update the usage line to `uv run FILE`. Semgrep does not parse script locks.
- Poetry 2.4 and later reads `min-release-age` in days under `[solver]` in `poetry.toml`.
- A bare `requirements.txt` with pip has no usable cooldown. Move it to `pyproject.toml` with a lockfile, unless AGENTS.md says not to.
- `astral-sh/setup-uv` without an exact `version:` installs the newest uv. Pin it.
- When Python moves, update `.python-version`, `requires-python`, the Dockerfile, CI `python-version`, and the targets of ruff and mypy.

**Go, Rust, Swift and others without a cooldown setting**

- Go: `go list -m -json MOD@VERSION` gives `.Time`. Pin with `go get MOD@VERSION`. A `go install TOOL@latest` in CI installs the newest release. Pin it to an exact `@vX.Y.Z`. When Go moves, update the `go` and `toolchain` lines, the Dockerfile and CI `go-version`.
- Cargo: `https://crates.io/api/v1/crates/NAME/VERSION` gives `version.created_at`. Pin with `cargo update -p NAME --precise VERSION`.
- RubyGems: `https://rubygems.org/api/v1/versions/NAME.json` gives `created_at`.
- SwiftPM and any dependency on GitHub: the release's `published_at`, or an annotated tag's tagger date. Never date a tag by its commit's date, because a new tag can point at an old commit. When neither date exists, the version cannot be dated. Adopt the Swift 6 language mode where it builds clean. Concurrency errors that need a redesign become a hold.

**Nix and devenv**

- A committed `flake.lock` or `devenv.lock` is the pin. Update it with the tool's own command and date the inputs it changes.
- A lock that `.gitignore` excludes pins nothing. Put it under "Needs you".
- When the repo has a `devenv.nix` or `flake.nix`, install Nix and devenv on the host, build the environment, and run the checks inside it, for example with `devenv shell -- COMMAND` or `devenv test`.
- When the Nix environment and the project disagree on a runtime, for example Python 3.13 in `devenv.nix` and 3.15 in `pyproject.toml`, never commit a change that the Nix environment cannot build. Put the mismatch under "Needs you".

**CI and services**

- Dependabot reads `cooldown:` with `default-days: 7` on each `updates` entry. Renovate reads `"minimumReleaseAge": "7 days"`.
- Semgrep's `github-actions-mutable-action-tag` rule flags a tag pin. Fix the pin. Do not triage the finding.
- Cloudflare Workers Builds ships its own npm and ignores `min-release-age`. Configure it in the Cloudflare dashboard and list it under "Needs you".
- `apt-get install` and `brew install` on hosted runners take whatever the runner image's package index holds, outside every cooldown. So does a `-latest` runner label, which moves to a new OS image. Neither can be pinned by digest. Keep the repo's runner label. These floats are expected, and so is an image tag that floats inside its digest pin. Do not list them in the PR unless Settings ask for them. The same applies to MSYS2 on Windows runners.
