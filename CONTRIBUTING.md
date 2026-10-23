# Contributing to Sadr

Thank you for your help. This file says how to send a change.

## Sign off every commit

Sadr accepts contributions under the
[Developer Certificate of Origin](https://developercertificate.org) (DCO).
The DCO says that you wrote the change, or have the right to submit it under
the project's license.

Every commit must carry a `Signed-off-by` line with your name and email:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds the line for you. CI checks every commit in a pull
request. A commit without the line fails the check. To sign off commits you
already made, run `git rebase --signoff main` on your branch and push it
again.

## Run the tests

The tests need Python 3.10 or later and no other packages:

```
python3 -m unittest discover tests
```

Run them before you open a pull request. CI runs them on every pull request.

## Write like Sadr

Sadr's text follows `plugins/sadr/skills/sadr/references/writing.md`. Use
the same rules for documentation and for the skill: active voice, short
sentences, no semicolons, and one name for one thing.

## Change the policy with care

The policy in `plugins/sadr/skills/sadr/references/` changes what happens in
every adopter's repos, so it follows semantic versioning:

- A fix or a clarification is a patch release.
- A new rule or a stricter rule is a minor release.
- A looser rule or a removed rule is a major release.

Add each policy change to `CHANGELOG.md` under the next release.

## Report a vulnerability

Do not open a public issue for a security problem. See
[SECURITY.md](SECURITY.md).
