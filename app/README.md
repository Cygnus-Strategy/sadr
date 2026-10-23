# Your Sadr App

Sadr acts on GitHub as a GitHub App that you own. You create your own copy of the App from the manifest in this folder, install it on your organization or user account, and keep its private key in your own secrets. Nobody else, Cygnus Strategy included, holds a key to your App. It is just between you and your AI provider.

The App is only an identity. It has no webhook, no server and no code. Every rule lives in the Sadr plugin and its workflows. The manifest changes only when Sadr needs a different permission, and the changelog says so.

## What is in this folder

- `manifest.json`: the [GitHub App manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest). It sets the name, the homepage, a turned-off webhook, and the permissions below. It subscribes to no events. `public` is true, so that you can install one App on other accounts that you own.
- `create.html`: a static page that sends the manifest to GitHub for the account you name.
- `../assets/app/sadr-app-logo.png`: the App's logo, 512 by 512 pixels. GitHub does not take a logo through the manifest. Upload it by hand.

## Permissions

The manifest asks for these repository permissions and nothing else. [references/permissions.md](../plugins/sadr/skills/sadr/references/permissions.md) says why Sadr needs each one.

| Permission | Access |
|---|---|
| Contents | read and write |
| Pull requests | read and write |
| Issues | read and write |
| Workflows | read and write |
| Checks | read |
| Commit statuses | read |
| Metadata | read |

## Create the App

### With the page

Serve this folder over HTTP, for example with `python3 -m http.server` in `app/`, and open `create.html`. Enter your organization, or choose a personal account. App names are unique across GitHub, and `Sadr` is reserved by a user account. Leave the name empty to get `Sadr for` and your account name, or pick another free name. Cygnus Strategy's own App is `sadrbot`. Sadr's commits show the App's bot user, such as `sadrbot[bot]`, as their author and are Verified: GitHub signs commits made through the API only under the App's own identity.

GitHub shows the App it will create. Check the permissions, then create it. GitHub sends a one-time code back to the page, and the page prints the commands that store the secrets. The code expires after one hour.

Opened as a local file, the page cannot receive the code. GitHub then shows the new App's settings, and you generate a private key there, as "By hand" says.

### By hand

The manifest flow is two requests:

1. Send a form `POST` to `https://github.com/organizations/ORG/settings/apps/new`, or to `https://github.com/settings/apps/new` for a personal account, with one field, `manifest`, that holds the JSON of `manifest.json`. Add `redirect_url` to the JSON when you want the code sent to a page you control. GitHub shows the App, and you confirm it.
2. GitHub redirects to `redirect_url` with `?code=CODE`. Exchange the code within one hour with `POST /app-manifests/CODE/conversions`. The response holds the App's `id`, `slug`, `pem` (the private key) and `html_url`.

Without a `redirect_url`, skip step 2. Open the App's settings, and under "Private keys" choose "Generate a private key". Store the downloaded file as below, then delete it.

## Store the secrets

The Sadr workflows read two secrets: `SADR_APP_ID` and `SADR_APP_PRIVATE_KEY`. Store them without printing them. For an organization, use organization secrets:

```bash
app=$(gh api -X POST /app-manifests/CODE/conversions)
jq -r .id <<<"$app" | gh secret set SADR_APP_ID --org ORG --visibility all
jq -r .pem <<<"$app" | gh secret set SADR_APP_PRIVATE_KEY --org ORG --visibility all
jq -r .html_url <<<"$app"
unset app
```

With a downloaded key file instead:

```bash
gh secret set SADR_APP_ID --org ORG --visibility all --body APP_ID
gh secret set SADR_APP_PRIVATE_KEY --org ORG --visibility all < ~/Downloads/KEY.private-key.pem
rm ~/Downloads/KEY.private-key.pem
```

Use `--visibility selected --repos .github` when only the organization-wide workflow in the `.github` repo runs Sadr. A personal account has no account-wide secrets. Set them on each repo that runs Sadr with `--repo OWNER/NAME` instead of `--org`.

Never paste the key into a chat, an issue, a PR or a log.

## Finish the App

1. In the App's settings, under "Display information", upload `assets/app/sadr-app-logo.png`.
2. Choose "Install App", pick your account, and install it on all repositories. To limit it, install it on the repos Sadr works on, the account's `.github` repo, and every repo that another repo lists under `### Related repos`.
3. Optional: grant Administration (read) when your repos use classic branch protection. Without it, Sadr cannot see that protection and leaves auto-merge off. Rulesets need no extra permission.

## Rotate or remove

Rotate the key from the App's settings when you suspect a run was compromised: generate a new key, store it as above, then delete the old one. To stop Sadr everywhere, uninstall the App or delete it.
