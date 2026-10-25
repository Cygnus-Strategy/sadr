# AGENTS.md

## Sadr

### Build and test
- Run `python3 -m unittest discover tests` with Python 3.10 or later. The tests use the standard library only. Passing means every test passes.
- Run `python3 .github/scripts/check-release.py` and `bash -n .github/scripts/app-token.sh`. Both exit 0 when they pass.
- Run `python3 plugins/sadr/skills/sadr/scripts/pins.py check .github/workflows/*.yml` with an authenticated `gh`. It exits 0 when every pin passes.
- `examples/sadr.yml` and `examples/sadr-org.yml` call Sadr at the placeholder `@SHA` on purpose, for adopters to replace. `pins.py check` flags both lines.

### Toolchain
- The scripts and tests use only the Python standard library. The repo has no package manager, no manifest and no lockfile.
- The CI test matrix in `.github/workflows/ci.yml` runs the oldest and the newest Python that the scripts support.

### Security notes
- Semgrep rule `skills.code-injection.skill-ldap-injection.skill-ldap-injection` flags the `re.search` calls in `branch_names_issue` in `scripts/issues.py`. They match branch names with regular expressions. No LDAP query exists in the repo.
- Semgrep rule `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected` flags `urlopen` in `semgrep_api.py`, `pins.py`, `osv.py` and `lockage.py`. Each one opens a constant https base URL or refuses a URL whose scheme is not https.
