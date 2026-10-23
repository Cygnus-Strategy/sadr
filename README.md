<p align="center"><img src="assets/banner.png" alt="Sadr: build in the dark" width="880"></p>

<p align="center">
  <a href="LICENSE"><img alt="license: BSD 3-Clause" src="https://img.shields.io/badge/license-BSD%203--Clause-15191f"></a>
  <!-- Add at launch, once the repo is public and v1.0.0 is tagged:
  <a href="https://github.com/Cygnus-Strategy/sadr/releases"><img alt="release" src="https://img.shields.io/github/v/release/Cygnus-Strategy/sadr?color=15191f"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/Cygnus-Strategy/sadr"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/Cygnus-Strategy/sadr/badge"></a>
  -->
  <a href="https://code.claude.com"><img alt="runs on Claude Code" src="https://img.shields.io/badge/runs%20on-Claude%20Code-15191f"></a>
</p>

<p align="center">
  <a href="https://sadr.cygnusstrategy.com">Website</a> •
  <a href="docs/agents-md.md">AGENTS.md conventions</a> •
  <a href="https://github.com/Cygnus-Strategy/sadr/security/policy">Security</a> •
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center"><em><strong>Sadr</strong> (pronounced SAD-er, /ˈsædər/) is the bright star at the center of the Northern Cross, the heart of the constellation Cygnus.</em></p>

## What is Sadr?

Sadr is a dark factory for software. It works through every repo you own with the lights off, keeping each one current, secure and up to your standards, and building the features waiting in your issues. People set the standard and review what comes out. Nothing in between waits on them.

Cygnus Strategy builds its software with Sadr. It's open source, so you can run your own.

## From idea to production

1. You tell Claude the idea, and it files an enhancement issue.
2. The nightly Sadr run builds it, alongside keeping dependencies, runtimes and CI current.
3. The pull request merges through your required checks.
4. Your CI deploys it. You never sat through a build session.

## What it does

- Builds the enhancement issues a maintainer has accepted.
- Keeps dependencies, runtimes, toolchains and CI current, majors included.
- Hardens the supply chain: releases wait before they're used, and actions and images are pinned.
- Fixes what security scanners find, in the pull request.
- Writes down what it learns about each repo in its AGENTS.md, in a PR of its own.
- Runs on your schedule, within time and spend limits, and merges only when you let it.

## Install

Sadr runs three ways. Pick one.

1. **Claude Code plugin.** Run Sadr from your own machine, by hand or as a Claude Code scheduled task. The plugin updates itself from this repo.

   ```
   /plugin marketplace add Cygnus-Strategy/sadr
   /plugin install sadr@sadr
   ```

   Then ask it to preview Sadr on an org, a list of repos, or one repo. A real run acts as your own Sadr App, so set up option 3 first.

2. **GitHub Action.** Call Sadr's reusable workflow on a schedule, pinned to a release. Sadr's upkeep PR moves the pin to newer releases after the cooldown.

   ```yaml
   on:
     schedule: [{cron: "17 3 * * *"}]   # nightly
   jobs:
     sadr:
       uses: Cygnus-Strategy/sadr/.github/workflows/run.yml@<sha> # v1.0.0
       with:
         sadr-ref: <sha>
       secrets:
         app_id: ${{ secrets.SADR_APP_ID }}
         app_private_key: ${{ secrets.SADR_APP_PRIVATE_KEY }}
         claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
   ```

   [examples/](examples/) has the full workflows for one repo and for a whole organization or user account.

3. **Your own Sadr App.** Sadr commits, opens PRs and comments as a GitHub App that you create from [app/manifest.json](app/manifest.json) and own. Its private key stays in your secrets, so nobody else can act as your Sadr. Use it with the Action or the plugin. Without it, the Action runs in a degraded mode that cannot change workflow files or read other repos.

<details>
<summary>Manual setup of the Sadr App</summary>

1. Serve [app/](app/) over HTTP (`python3 -m http.server` in that folder) and open `create.html`. Enter your organization, or choose a personal account.
2. Name the App `Sadr for` and your account name, or another free name. App names are unique across GitHub, and `Sadr` is reserved.
3. On GitHub, check that the permissions are exactly: Contents, Pull requests, Issues and Workflows read and write, and Checks, Commit statuses and Metadata read. Create the App.
4. Run the commands the page prints. They store `SADR_APP_ID` and `SADR_APP_PRIVATE_KEY` as organization secrets without printing them.
5. In the App's settings, under "Display information", upload [assets/app/sadr-app-logo.png](assets/app/sadr-app-logo.png).
6. Install the App on your account, on all repositories.
7. Add the workflow from option 2, with a credential for Claude.

[app/README.md](app/README.md) has the manifest flow request by request, the commands for a personal account, and key rotation. [references/permissions.md](plugins/sadr/skills/sadr/references/permissions.md) explains Sadr's identity and tokens.

</details>

### Agent instructions

Paste this into an AI agent that can drive a web browser and run `gh`, to set up the App for you:

```text
Set up the Sadr GitHub App for the GitHub account OWNER (an organization unless I say it is a personal account). Use a web browser for the GitHub pages and the gh CLI for secrets. Ask me before any step that needs my password or two-factor code, and let me type those myself.

1. Clone https://github.com/Cygnus-Strategy/sadr. Read app/README.md and app/manifest.json in full.
2. Serve the app/ folder over HTTP on localhost and open create.html in the browser. Enter OWNER. Use the App name "Sadr for OWNER". If GitHub says the name is taken, pick another free name.
3. On GitHub's confirmation page, check that the repository permissions are exactly: Contents, Pull requests, Issues and Workflows read and write; Checks, Commit statuses and Metadata read; nothing else, no webhook, no events. If they differ, stop and tell me. Otherwise create the App.
4. Run the commands that create.html prints. They store SADR_APP_ID and SADR_APP_PRIVATE_KEY as secrets. Never print, copy or show the private key, and never write it to a file outside a temporary one you delete at once.
5. In the App's settings, under Display information, upload assets/app/sadr-app-logo.png from the clone as the logo.
6. Install the App on OWNER, on all repositories.
7. Confirm with: gh secret list --org OWNER (or --repo for a personal account). Report the App's URL, its slug, and where it is installed. Do not report the key.
```

Then tell Sadr about each repo in plain language, in the `## Sadr` section of its AGENTS.md. See [docs/agents-md.md](docs/agents-md.md).

## Design principles

- **Lights out.** It runs unattended, on your schedule.
- **Stateless.** What it knows lives in your repos and on GitHub.
- **Language over configuration.** Settings are sentences in AGENTS.md.
- **Fix it, don't file it.** Real problems are fixed in the pull request.
- **Secure by default.** And it meets its own standard.
- **People decide what ships.** Nothing merges unless you merge it or told it to.
- **Nothing to do, nothing sent.** A repo that meets the standard gets no PR and no comment.

## License

BSD 3-Clause. Made by [Cygnus Strategy LLC](https://cygnusstrategy.com). Contributions are welcome under the DCO. See [CONTRIBUTING.md](CONTRIBUTING.md).
