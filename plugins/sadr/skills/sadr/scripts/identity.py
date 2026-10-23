#!/usr/bin/env python3
"""Print the git identity that Sadr commits as.

A real run acts as the organization's own copy of the Sadr GitHub App.
Its commits use the App's bot account:

  name   Sadr
  email  <bot user id>+<app slug>[bot]@users.noreply.github.com

Each organization's App has its own slug and bot user id. The script looks
up the id at run time with `gh api /users/<slug>%5Bbot%5D`, so nothing is
hard-coded. The slug comes from --app-slug, or else from SADR_APP_SLUG.

A local preview pushes nothing. When the clone has no git identity of its
own, --placeholder prints a placeholder identity for the local commits,
without a network call: "Sadr (preview)" and sadr-preview@localhost.invalid.

--format text prints the name and the email on two lines. --format env
prints GIT_AUTHOR_* and GIT_COMMITTER_* assignments for `export`. --format
git prints the `-c user.name=... -c user.email=...` arguments for git.
--format json prints both fields.

Examples:
  identity.py --app-slug sadr-acme
  eval "$(identity.py --app-slug sadr-acme --format env)"
  git $(identity.py --placeholder --format git) commit -m "..."

Needs an authenticated `gh` CLI, except with --placeholder. The script
never prints the token. Standard library only.
"""
import argparse
import json
import os
import re
import shlex
import sys

import _gh

NAME = "Sadr"
PLACEHOLDER = {"name": "Sadr (preview)", "email": "sadr-preview@localhost.invalid"}
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", re.I)


def bot_identity(slug, name=NAME):
    """Return {name, email, login, id} for the bot user of the App SLUG.

    Raise LookupError when GitHub has no such bot user.
    """
    if not SLUG.fullmatch(slug or ""):
        raise ValueError(f"not an App slug: {slug!r}")
    login = f"{slug}[bot]"
    user = _gh.api(f"/users/{slug}%5Bbot%5D")
    if not isinstance(user, dict) or not user.get("id"):
        raise LookupError(f"GitHub has no user {login}. Check the App slug (the last part of the App's URL, "
                          f"github.com/apps/<slug>)")
    return {"name": name, "email": f"{user['id']}+{login}@users.noreply.github.com", "login": login,
            "id": user["id"]}


def render(ident, fmt):
    if fmt == "json":
        return json.dumps(ident, indent=2)
    if fmt == "env":
        return "\n".join(f"{k}={shlex.quote(ident[f])}" for k, f in (
            ("GIT_AUTHOR_NAME", "name"), ("GIT_AUTHOR_EMAIL", "email"),
            ("GIT_COMMITTER_NAME", "name"), ("GIT_COMMITTER_EMAIL", "email")))
    if fmt == "git":
        return " ".join(shlex.quote(x) for x in ("-c", f"user.name={ident['name']}",
                                                 "-c", f"user.email={ident['email']}"))
    return f"{ident['name']}\n{ident['email']}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--app-slug", default=os.environ.get("SADR_APP_SLUG"),
                    help="the App's slug (default: $SADR_APP_SLUG)")
    ap.add_argument("--name", default=NAME, help=f"the author name (default: {NAME})")
    ap.add_argument("--placeholder", action="store_true",
                    help="print the placeholder identity for local preview commits, with no network call")
    ap.add_argument("--format", choices=["text", "env", "git", "json"], default="text")
    ap.add_argument("--json", action="store_true", help="same as --format json")
    a = ap.parse_args(argv)
    fmt = "json" if a.json else a.format
    if a.placeholder:
        print(render(dict(PLACEHOLDER), fmt))
        return 0
    if not a.app_slug:
        ap.error("pass --app-slug or set SADR_APP_SLUG (or --placeholder for a preview)")
    try:
        ident = bot_identity(a.app_slug, a.name)
    except (ValueError, LookupError, _gh.GhError) as e:
        print(f"identity.py: {e}", file=sys.stderr)
        return 2
    print(render(ident, fmt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
