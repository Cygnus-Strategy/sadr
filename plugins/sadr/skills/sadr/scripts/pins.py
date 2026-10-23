#!/usr/bin/env python3
"""Check and resolve the pins of GitHub Actions, container images and tools.

Sadr pins every action and reusable workflow to the full commit SHA of a
release that is at least 7 days old, with the exact version as a comment:

  uses: actions/checkout@<40-character SHA> # v5.0.0

Sadr pins every container image to a digest that is at least 7 days old,
with the tag in front of it:

  FROM python:3.15-slim@sha256:<64 hex characters>

`check` reads workflow YAML, composite actions, compose files and
Dockerfiles. It reports each line with a problem:

- an action ref that is not a 40-character commit SHA
- an action line with no `# vX.Y.Z` comment, or a comment that is not an
  exact version
- an action SHA that is not the commit that the tag in the comment points to
- an action release younger than --min-age-days
- an action tag that Sadr cannot date: no release, and a lightweight tag
- an image in Dockerfile FROM or COPY --from, compose `image:`, workflow
  `container:` or `services:`, or `uses: docker://` with no digest, no tag,
  or a digest younger than --min-age-days
- a setup-* action input that ends in "version" and is not an exact
  version, or `check-latest: true`
- `npx`, `bunx`, `pnpm dlx`, `yarn dlx`, `uvx` or `pipx run` with a
  version range, `@latest`, or (for uvx and pipx) no version
- `go install` or `go run` of a module at @latest, a branch, or no
  version
- a setup action that installs the newest release when its version input
  is missing, such as astral-sh/setup-uv without `version` or
  `version-file`
- a .python-version, .node-version, .nvmrc, .go-version or
  .tool-versions file whose version is not exact

`check` also reads shell scripts and Makefiles for the installer calls.

`check` lists what it skipped and why: local actions, values set by an
expression or a build argument, and `npx` without a version. A registry
that keeps answering HTTP 429 leaves an image "not checked: rate
limited", which is not a problem with the pin. `check` exits 1 when it
finds a problem, 3 when it found none but could not check some images,
and 0 otherwise.

`resolve` finds the newest release of an action that is not a draft, not a
prerelease, and at least --min-age-days old. It sorts releases by version,
not by date. Without releases, it uses version tags dated by their tagger
date. It never dates a tag by its commit date.

`image resolve` finds the newest digest of an image tag that is at least
--min-age-days old. For Docker Hub official images it reads the digest
history in docker-library/repo-info. For other images it reads the
registry, which keeps only the current digest of a tag.

`image newest` finds the newest exact version tag of an image (such as
1.181.0, or 3.15.1-slim with --variant slim) that has a digest at least
--min-age-days old, and prints its pin. It walks down the versions until
one qualifies.

`runtime` says what a runtime version spec resolves to today, and what to
pin. A floating spec (node 24, python 3.15, go stable) is already in use
at what it resolves to today, so the pin is that version, never a lower
one, even when it is inside the cooldown: pin_inside_cooldown then says
so, and security says whether that release is a security release (Node
and Go publish this; Python does not, so it is null). For an exact spec,
the pin is the newest release on the same line past the cooldown, never
below the spec. latest_security names the newest security release on the
line. Supported: node (nodejs.org/dist), python (the versions that
actions/setup-python installs, dated by python.org) and go (go.dev).

Examples:
  pins.py check .github/workflows/*.yml Dockerfile compose.yaml
  pins.py resolve actions/checkout
  pins.py --json resolve github/codeql-action/init --min-age-days 14
  pins.py image resolve python:3.15-slim
  pins.py image newest semgrep/semgrep --min-age-days 7
  pins.py image newest python --variant slim --line 3.15
  pins.py runtime node 24
  pins.py --json runtime python 3.15.0
  pins.py runtime go stable

Needs an authenticated `gh` CLI, and network access to the image
registries. Standard library only.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request

import _gh
import _registry

DEFAULT_MIN_AGE = 7
SHA = re.compile(r"[0-9a-f]{40}")
USES = re.compile(
    r"""^(?P<indent>\s*(?:-\s+)?)uses:\s*(?P<q>['"]?)(?P<target>[^'"\s#]+)(?P=q)\s*(?:\#\s*(?P<comment>.*?))?\s*$""")
VERSION = re.compile(r"v?\d+(?:\.\d+)*")
EXACT = re.compile(r"v?\d+\.\d+\.\d+(?:\+[\w.]+)?")
IMAGE_KEY = re.compile(r"""^\s*(?:-\s+)?(?P<key>image|container):\s*(?P<value>[^#]*?)\s*(?:\s#.*)?$""")
SETUP_INPUT = re.compile(r"""^\s*(?P<key>[\w-]*version|check-latest):\s*(?P<value>[^#]*?)\s*(?:\s#.*)?$""")
FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.I)
COPY_FROM = re.compile(r"^\s*COPY\b.*?--from=(?P<image>\S+)", re.I)
INSTALLER = re.compile(r"(?:^|[\s;&|(`$])(?P<tool>npx|bunx|uvx|pipx\s+run|pnpm\s+dlx|yarn\s+dlx|go\s+install|go\s+run)"
                       r"\s+(?P<rest>[^;&|\n]*)")
# Setup actions that install the newest release when none of these inputs is set.
LATEST_WITHOUT = {"astral-sh/setup-uv": ("version", "version-file"),
                  "oven-sh/setup-bun": ("bun-version", "bun-version-file")}
VERSION_FILES = (".python-version", ".node-version", ".nvmrc", ".go-version", ".tool-versions",
                 ".ruby-version", ".terraform-version")
GO_VERSION = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?|[0-9a-f]{40}")
# Options of these tools that take the package spec as their value.
SPEC_OPTIONS = {"--from", "-p", "--package", "--spec"}
VALUE_OPTIONS = {"--with", "--python", "-c", "--call", "--index", "--index-url", "--registry"}


def strip_value(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v.strip()


def is_dockerfile(path):
    base = os.path.basename(path).lower()
    return base.startswith(("dockerfile", "containerfile")) or base.endswith((".dockerfile", ".containerfile"))


def image_row(path, n, ref, where):
    """One row for an image reference, or a skipped row."""
    row = {"file": path, "line": n, "kind": "image", "uses": ref, "where": where}
    if "$" in ref:
        row["skip"] = "set by an expression or a build argument"
    elif ref.lower() == "scratch":
        row["skip"] = "scratch is not an image"
    return row


def parse_uses(path, text):
    """Return one dict for each `uses:` line in a YAML text."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = USES.match(line)
        if not m:
            continue
        target, comment = m.group("target"), (m.group("comment") or "").strip()
        if target.startswith("docker://"):
            out.append(image_row(path, n, target[len("docker://"):], "uses: docker://"))
            continue
        row = {"file": path, "line": n, "kind": "action", "uses": target, "comment": comment}
        if target.startswith("./"):
            row["skip"] = "local action"
        elif "${{" in target:
            row["skip"] = "set by an expression"
        else:
            name, _, ref = target.partition("@")
            parts = name.split("/")
            row["action"] = "/".join(parts[:2])
            row["ref"] = ref
            v = VERSION.match(comment)
            row["version"] = v.group(0) if v else ""
        out.append(row)
    return out


def parse_yaml_images(path, text):
    """Rows for `image:` and string `container:` values in a YAML text."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = IMAGE_KEY.match(line)
        if not m:
            continue
        value = strip_value(m.group("value"))
        if not value or value in ("|", ">"):
            continue
        out.append(image_row(path, n, value, m.group("key") + ":"))
    return out


def parse_setup_inputs(path, text):
    """Rows for the version inputs of setup-* actions in a workflow."""
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = USES.match(line)
        if not m:
            continue
        action = m.group("target").split("@")[0]
        if not re.search(r"(^|/)setup-[\w-]+$", action):
            continue
        key_indent = len(m.group("indent"))
        seen_keys = set()
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent < key_indent or (indent == key_indent and nxt.lstrip().startswith("-")):
                break
            k = re.match(r"\s*([\w-]+):", nxt)
            if k and indent > key_indent:
                seen_keys.add(k.group(1))
            s = SETUP_INPUT.match(nxt)
            if not s or indent <= key_indent:
                continue
            key, value = s.group("key"), strip_value(s.group("value"))
            row = {"file": path, "line": j + 1, "kind": "setup", "uses": f"{action} {key}: {value}",
                   "action": action, "input": key, "value": value}
            if key == "check-latest":
                row["problems"] = ["check-latest: true installs the newest release"] if value.lower() == "true" else []
            elif "${{" in value:
                row["skip"] = "set by an expression"
            elif value in ("|", ">", "|-", ">-", ""):
                row["skip"] = "a multi-line value"
            else:
                row["problems"] = [] if EXACT.fullmatch(value) else [
                    f"{key} '{value}' is not an exact version (X.Y.Z)"]
            out.append(row)
        needs = LATEST_WITHOUT.get(action.lower())
        if needs and not seen_keys & set(needs):
            out.append({"file": path, "line": i + 1, "kind": "setup", "uses": f"{action} (no {needs[0]})",
                        "action": action, "input": needs[0], "value": None,
                        "problems": [f"{action} without {' or '.join(needs)} installs the newest release. "
                                     f"Set {needs[0]} to an exact X.Y.Z"]})
    return out


def parse_version_file(path, text):
    """Rows for runtime version files such as .python-version and .tool-versions."""
    out, base = [], os.path.basename(path)
    for n, line in enumerate(text.splitlines(), 1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        tool = None
        if base == ".tool-versions":
            parts = value.split()
            if len(parts) < 2:
                continue
            tool, value = parts[0], parts[1]
        row = {"file": path, "line": n, "kind": "runtime", "uses": f"{tool + ' ' if tool else ''}{value}",
               "value": value}
        if value.startswith(("system", "ref:", "path:")):
            row["skip"] = f"{value.split(':')[0]} is not a version"
        else:
            v = value[1:] if value[:1] in "vV" else value
            v = re.sub(r"^(python|node|nodejs|go)-?", "", v)
            row["problems"] = [] if EXACT.fullmatch(v) else [
                f"'{value}' is not an exact version (X.Y.Z), so the runtime moves with each release"]
        out.append(row)
    return out


def split_spec(tool, spec):
    """Return (package, version or None) for an installer's package spec."""
    if tool in ("go install", "go run"):
        pkg, sep, version = spec.partition("@")
        return pkg, (version if sep else None)
    if tool in ("uvx", "pipx run"):
        m = re.match(r"([A-Za-z0-9_.\[\],-]+?)\s*(==|@|>=|<=|~=|>|<|!=)\s*(.+)", spec)
        if m:
            return m.group(1), ("" if m.group(2) == "==" or m.group(2) == "@" else m.group(2)) + m.group(3)
        return spec, None
    at = spec.rfind("@")
    if at > 0:
        return spec[:at], spec[at + 1:]
    return spec, None


def parse_installers(path, text):
    """Rows for npx, bunx, pnpm dlx, yarn dlx, uvx and pipx run calls."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in INSTALLER.finditer(line):
            tool = re.sub(r"\s+", " ", m.group("tool"))
            try:
                tokens = shlex.split(m.group("rest"), comments=True)
            except ValueError:
                tokens = m.group("rest").split()
            spec, k = None, 0
            while k < len(tokens):
                t = tokens[k]
                if t in SPEC_OPTIONS and k + 1 < len(tokens):
                    spec = tokens[k + 1]
                    k += 2
                    continue
                if t.split("=")[0] in SPEC_OPTIONS and "=" in t:
                    spec = t.split("=", 1)[1]
                elif t in VALUE_OPTIONS:
                    k += 1
                elif not t.startswith("-") and spec is None:
                    spec = t
                    break
                elif not t.startswith("-"):
                    break
                k += 1
            if not spec or "${{" in spec or "$" in spec:
                continue
            if tool in ("go install", "go run") and (spec.startswith((".", "/")) or spec.endswith(".go")
                                                    or "." not in spec.split("/")[0]):
                # A local package or file, built from the repo's own go.mod.
                continue
            pkg, version = split_spec(tool, spec)
            row = {"file": path, "line": n, "kind": "installer", "uses": f"{tool} {spec}", "tool": tool,
                   "package": pkg, "version": version}
            if tool in ("go install", "go run"):
                if version is None:
                    row["skip"] = f"{tool} {pkg} has no version, so it uses the version in go.mod"
                elif version == "latest" or not GO_VERSION.fullmatch(version):
                    row["problems"] = [f"{tool} {pkg}@{version} is not an exact version (vX.Y.Z), so it "
                                       "installs a fresh release"]
                else:
                    row["problems"] = []
            elif version is None:
                if tool in ("uvx", "pipx run"):
                    row["problems"] = [f"{tool} {pkg} has no version, so it installs the newest release"]
                else:
                    row["skip"] = (f"{tool} {pkg} has no version. It uses the local install when {pkg} is a "
                                   "locked dependency, or else the newest release")
            elif version == "latest" or not EXACT.fullmatch(version):
                row["problems"] = [f"{tool} {pkg}@{version} is not an exact version, so it installs a fresh release"]
            else:
                row["problems"] = []
            out.append(row)
    return out


def parse_dockerfile(path, text):
    """Rows for FROM and COPY --from images in a Dockerfile."""
    out, stages = [], set()
    for n, line in enumerate(text.splitlines(), 1):
        m = FROM.match(line)
        if m:
            ref = m.group("image")
            if ref.lower() in stages:
                pass
            else:
                out.append(image_row(path, n, ref, "FROM"))
            if m.group("stage"):
                stages.add(m.group("stage").lower())
            continue
        m = COPY_FROM.match(line)
        if m:
            ref = m.group("image")
            if ref.lower() not in stages and not ref.isdigit():
                out.append(image_row(path, n, ref, "COPY --from"))
    return out


def is_script(path):
    base = os.path.basename(path)
    return base.endswith((".sh", ".bash", ".zsh", ".mk")) or base in ("Makefile", "makefile", "GNUmakefile",
                                                                       "justfile", "Justfile", "Taskfile")


def parse_file(path, text):
    """All pin rows of one file, in line order."""
    if os.path.basename(path) in VERSION_FILES:
        return parse_version_file(path, text)
    if is_script(path):
        return parse_installers(path, text)
    if is_dockerfile(path):
        rows = parse_dockerfile(path, text) + parse_installers(path, text)
    else:
        rows = (parse_uses(path, text) + parse_yaml_images(path, text) + parse_setup_inputs(path, text)
                + parse_installers(path, text))
    return sorted(rows, key=lambda r: r["line"])


class Resolver:
    """Look up tags, releases and image dates, with a cache."""

    def __init__(self, registry=None):
        self._tags = {}
        self._images = {}
        self.registry = registry or _registry.Client()

    def tag(self, action, tag):
        """Return (commit SHA, date, source) for an action's tag. See _gh.tag_date."""
        key = (action, tag)
        if key not in self._tags:
            self._tags[key] = _gh.tag_date(action, tag)
        return self._tags[key]

    def image_created(self, ref):
        """Return (digest, created) for an image reference, or raise RegistryError."""
        if ref not in self._images:
            p = _registry.parse_ref(ref)
            try:
                self._images[ref] = self.registry.created(p["registry"], p["repo"], p["digest"] or p["tag"] or "latest")
            except _registry.RegistryError as e:
                self._images[ref] = e
        if isinstance(self._images[ref], Exception):
            raise self._images[ref]
        return self._images[ref]


def action_problems(row, resolver, min_age, at=None):
    """Return the list of problems with one parsed action `uses:` row."""
    ref, version = row["ref"], row["version"]
    if not SHA.fullmatch(ref):
        return [f"not pinned to a 40-character commit SHA (@{ref or 'no ref'})"]
    if not row["comment"] or not version:
        return ["no '# vX.Y.Z' version comment"]
    problems = []
    if _gh.semver(version) is None:
        problems.append(f"version comment {version} is not an exact vX.Y.Z")
    sha, when, source = resolver.tag(row["action"], version)
    if not sha:
        return problems + [source]
    if sha != ref:
        # This also catches a SHA that exists only in a fork of the action.
        problems.append(f"SHA {ref[:7]} is not {row['action']}@{version} ({sha[:7]})")
    if not when:
        problems.append(f"{version}: {source}")
    else:
        age = _gh.age_days(when, at)
        if age < min_age:
            problems.append(f"{version} is {age:.1f} days old, younger than {min_age:g} days")
    return problems


def image_problems(row, resolver, min_age, at=None):
    """Return the list of problems with one image row."""
    p = _registry.parse_ref(row["uses"])
    if not p["digest"]:
        return [f"not pinned to a digest (use {row['uses'] if p['tag'] else row['uses'] + ':TAG'}@sha256:...)"]
    problems = []
    if not p["tag"]:
        problems.append("no tag in front of the digest, so a reader cannot see the version")
    try:
        _, created = resolver.image_created(row["uses"])
    except _registry.RateLimited:
        raise
    except _registry.RegistryError as e:
        return problems + [f"cannot date the digest: {e}"]
    age = _gh.age_days(created, at)
    if age < min_age:
        problems.append(f"the digest was built {created[:10]}, {age:.1f} days ago, younger than {min_age:g} days")
    return problems


def problems_for(row, resolver, min_age, at=None):
    """Return the list of problems with one parsed row."""
    if row["kind"] == "image":
        return image_problems(row, resolver, min_age, at)
    return action_problems(row, resolver, min_age, at)


def check(paths, min_age=DEFAULT_MIN_AGE, resolver=None, at=None):
    """Check every pin in PATHS. Return the rows with their problems or skip reasons."""
    resolver = resolver or Resolver()
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for row in parse_file(path, text):
            if "skip" not in row and "problems" not in row:
                try:
                    row["problems"] = problems_for(row, resolver, min_age, at)
                except _registry.RateLimited as e:
                    row["problems"], row["not_checked"] = [], f"rate limited ({e})"
            rows.append(row)
    return rows


def pick_release(releases, min_age, at=None):
    """Return the newest eligible release by version, or None.

    Eligible means: not a draft, not a prerelease, an exact X.Y.Z tag, and
    published at least MIN_AGE days ago. A moving tag such as v4 is not
    eligible, because its commit changes.
    """
    ok = [r for r in releases
          if not r.get("draft") and not r.get("prerelease") and r.get("published_at")
          and _gh.semver(r.get("tag_name")) and _gh.age_days(r["published_at"], at) >= min_age]
    return max(ok, key=lambda r: _gh.semver(r["tag_name"])) if ok else None


def resolve(action, min_age=DEFAULT_MIN_AGE, at=None):
    """Return {action, tag, sha, published_at, source} for the newest eligible release.

    When the repo has no releases, use its version tags dated by the tagger
    date of an annotated tag. A lightweight tag is never eligible, and the
    result lists it under "undated".
    """
    repo = "/".join(action.split("/")[:2])
    releases = _gh.api(f"repos/{repo}/releases?per_page=100", paginate=True) or []
    if releases:
        rel = pick_release(releases, min_age, at)
        if not rel:
            return None
        commit = _gh.api(f"repos/{repo}/commits/{rel['tag_name']}") or {}
        return {"action": action, "tag": rel["tag_name"], "sha": commit.get("sha"),
                "published_at": rel["published_at"], "source": "release"}
    tags = _gh.api(f"repos/{repo}/tags?per_page=100", paginate=True) or []
    undated = []
    for t in sorted((t for t in tags if _gh.semver(t["name"])), key=lambda t: _gh.semver(t["name"]), reverse=True):
        sha, when, source = _gh.tag_date(repo, t["name"])
        if not when:
            undated.append(t["name"])
            continue
        if _gh.age_days(when, at) >= min_age:
            return {"action": action, "tag": t["name"], "sha": sha, "published_at": when, "source": source,
                    "undated": undated}
    return {"action": action, "tag": None, "undated": undated} if undated else None


REPO_INFO = "docker-library/repo-info"
PULL_LINE = re.compile(r"docker pull \S+@(sha256:[0-9a-f]{64})")


def repo_info_digest(name, tag, commit):
    """The digest that docker-library/repo-info lists for NAME:TAG at COMMIT, or None."""
    path = f"repos/{name}/remote/{tag}.md"
    raw = _gh.api(f"repos/{REPO_INFO}/contents/{path}?ref={commit}", raw=True)
    if not raw:
        return None
    m = PULL_LINE.search(raw)
    return m.group(1) if m else None


def resolve_official(name, tag, min_age, at=None):
    """Newest digest of library/NAME:TAG that docker-library/repo-info listed MIN_AGE days ago.

    The digest's date is the time of the oldest repo-info commit that lists
    it, among the commits checked. The walk stops at the first older digest.
    """
    at = at or _gh.now()
    cutoff = (at - datetime.timedelta(days=min_age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"repos/{name}/remote/{tag}.md"
    commits = _gh.api(f"repos/{REPO_INFO}/commits?path={path}&until={cutoff}&per_page=30") or []
    if not commits:
        return None
    digest, since = None, None
    for c in commits:
        d = repo_info_digest(name, tag, c["sha"])
        if digest is None:
            digest = d
        if d != digest:
            break
        since = c["commit"]["committer"]["date"]
    if not digest:
        return None
    return {"digest": digest, "published_at": since, "source": f"{REPO_INFO} history ({path})"}


def resolve_image(ref, min_age=DEFAULT_MIN_AGE, at=None, client=None):
    """Return the newest digest of IMAGE:TAG that is at least MIN_AGE days old.

    The result has image, tag, digest, published_at, source and pin. When no
    digest qualifies, digest is None and "reason" says why.
    """
    p = _registry.parse_ref(ref)
    tag = p["tag"] or "latest"
    shown = ref.split("@")[0] if p["tag"] else ref.split("@")[0] + ":latest"
    base = {"image": ref.split("@")[0].rsplit(":", 1)[0] if p["tag"] else ref.split("@")[0], "tag": tag}
    if p["official"]:
        res = resolve_official(p["repo"][len("library/"):], tag, min_age, at)
        if res:
            return {**base, **res, "pin": f"{shown}@{res['digest']}"}
        return {**base, "digest": None, "reason": f"docker-library/repo-info has no record of {shown} "
                                                  f"from {min_age:g} or more days ago"}
    client = client or _registry.Client()
    try:
        digest, created = client.created(p["registry"], p["repo"], tag)
    except _registry.RegistryError as e:
        return {**base, "digest": None, "reason": str(e)}
    age = _gh.age_days(created, at)
    if age >= min_age:
        return {**base, "digest": digest, "published_at": created, "source": "registry config created",
                "pin": f"{shown}@{digest}"}
    return {**base, "digest": None, "current_digest": digest, "published_at": created,
            "reason": f"the current digest of {shown} was built {age:.1f} days ago. The registry keeps no "
                      "older digest for the tag. Pick an older exact tag, or wait"}


def tag_version(tag, variant=None):
    """'3.15.1-slim' -> (3, 15, 1) when the variant matches. None for other tags."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?", tag or "")
    if not m or (m.group(4) or None) != (variant or None):
        return None
    return tuple(int(x) for x in m.groups()[:3])


def newest_image(image, variant=None, line=None, min_age=DEFAULT_MIN_AGE, at=None, client=None, max_checks=15):
    """The newest exact version tag of IMAGE with a digest at least MIN_AGE days old.

    Return a dict with image, tag, digest, published_at, source, pin and
    the tags it passed over (too young or undatable). digest is None and
    reason says why when no tag qualifies within MAX_CHECKS tries.
    """
    client = client or _registry.Client()
    p = _registry.parse_ref(image.split("@")[0])
    name = image.split("@")[0]
    if p["tag"]:
        name = name.rsplit(":", 1)[0]
    try:
        tags = client.tags(p["registry"], p["repo"])
    except _registry.RegistryError as e:
        return {"image": name, "digest": None, "reason": f"cannot list tags: {e}"}
    want = [] if not line else [int(x) for x in line.split(".")]
    cands = []
    for t in tags:
        v = tag_version(t, variant)
        if v and list(v[:len(want)]) == want:
            cands.append((v, t))
    cands.sort(reverse=True)
    passed = []
    for v, t in cands[:max_checks]:
        res = resolve_image(f"{name}:{t}", min_age, at, client)
        if res.get("digest"):
            return {**res, "image": name, "tag": t, "passed_over": passed}
        passed.append({"tag": t, "reason": res.get("reason")})
    reason = (f"no {'-' + variant + ' ' if variant else ''}version tag" + (f" on line {line}" if line else "")
              + (f" among the newest {max_checks}" if len(cands) > max_checks else "")
              + f" has a digest at least {min_age:g} days old")
    return {"image": name, "digest": None, "reason": reason if cands else "no exact version tag matches",
            "passed_over": passed}


# Runtime releases. Each source returns [{version, date, security, lts}] for
# stable releases, newest first. Tests replace FETCH.
def fetch_url(url):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing a non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "sadr", "Accept": "*/*"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return f.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise LookupError(f"{url}: HTTP {e.code}") from None
            _registry.sleep(_registry.retry_after(e.headers, attempt))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise LookupError(f"{url}: {getattr(e, 'reason', e)}") from None
            _registry.sleep(2 ** attempt)
    raise AssertionError("unreachable")


FETCH = fetch_url


def node_releases():
    data = json.loads(FETCH("https://nodejs.org/dist/index.json"))
    out = []
    for r in data:
        v = _gh.semver(r.get("version"))
        if v:
            out.append({"version": ".".join(map(str, v)), "date": r.get("date") + "T00:00:00Z",
                        "security": bool(r.get("security")), "lts": r.get("lts") or None})
    return out


def python_releases():
    """Versions that actions/setup-python installs, dated by python.org."""
    manifest = json.loads(_gh.api("repos/actions/python-versions/contents/versions-manifest.json", raw=True)
                          or "[]")
    available = {m["version"] for m in manifest if m.get("stable") and _gh.semver(m.get("version"))}
    dates = {}
    for r in json.loads(FETCH("https://www.python.org/api/v2/downloads/release/?is_published=true")):
        m = re.fullmatch(r"Python (\d+\.\d+\.\d+)", r.get("name") or "")
        if m and r.get("release_date"):
            dates[m.group(1)] = r["release_date"]
    out = [{"version": v, "date": dates.get(v), "security": None, "lts": None} for v in available]
    return sorted(out, key=lambda r: _gh.semver(r["version"]), reverse=True)


GO_RELEASE = re.compile(r"\bgo(\d+\.\d+(?:\.\d+)?)\s*\(released\s*(\d{4}-\d{2}-\d{2})\s*\)")


def go_releases():
    """Go releases from go.dev/doc/devel/release, with their dates and security flags.

    The text after each "goX.Y.Z (released DATE)" up to the next one says
    whether the release "includes security fixes".
    """
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", FETCH("https://go.dev/doc/devel/release")))
    marks = list(GO_RELEASE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(marks):
        v = m.group(1) + (".0" if m.group(1).count(".") == 1 else "")
        if v in seen:
            continue
        seen.add(v)
        tail = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        out.append({"version": v, "date": m.group(2) + "T00:00:00Z",
                    "security": bool(re.search(r"security fix", tail[:600], re.I)), "lts": None})
    return sorted(out, key=lambda r: _gh.semver(r["version"]), reverse=True)


RUNTIMES = {"node": node_releases, "python": python_releases, "go": go_releases}


def spec_filter(runtime, spec, releases):
    """(releases the spec can resolve to, floating?, line) for a version spec."""
    s = spec.strip().lower().lstrip("v")
    if runtime == "go":
        s = s[2:] if s.startswith("go") else s
    if runtime == "node" and (s.startswith("lts/") or s in ("lts", "node", "latest", "current")):
        if s in ("node", "latest", "current"):
            return releases, True, None
        name = s.split("/", 1)[1] if "/" in s else "*"
        lts = [r for r in releases if r["lts"] and (name in ("*", "") or r["lts"].lower() == name)]
        if name in ("*", "") and lts:
            newest = _gh.semver(lts[0]["version"])[0]
            lts = [r for r in lts if _gh.semver(r["version"])[0] == newest]
        return lts, True, None
    if runtime == "go" and s in ("stable", "oldstable"):
        lines = sorted({_gh.semver(r["version"])[:2] for r in releases}, reverse=True)
        pick = lines[0] if s == "stable" else (lines[1] if len(lines) > 1 else None)
        return [r for r in releases if pick and _gh.semver(r["version"])[:2] == pick], True, \
            ".".join(map(str, pick)) if pick else None
    s = re.sub(r"\.x$|\.\*$", "", s)
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", s):
        raise ValueError(f"cannot read the {runtime} version spec {spec!r}")
    parts = [int(x) for x in s.split(".")]
    exact = len(parts) == 3
    line_len = 1 if runtime == "node" else 2
    line = ".".join(map(str, parts[:line_len]))
    if exact:
        same_line = [r for r in releases if list(_gh.semver(r["version"])[:line_len]) == parts[:line_len]]
        return same_line, False, line
    match = [r for r in releases if list(_gh.semver(r["version"])[:len(parts)]) == parts]
    return match, True, line if len(parts) >= line_len else None


def resolve_runtime(runtime, spec, min_age=DEFAULT_MIN_AGE, at=None, releases=None):
    """What a runtime version SPEC resolves to today, and the version to pin.

    A floating spec pins at what it resolves to today, never lower. An
    exact spec pins at the newest release on its line past MIN_AGE days,
    never below the spec.
    """
    at = at or _gh.now()
    if runtime not in RUNTIMES:
        raise ValueError(f"unsupported runtime {runtime!r}: use one of {', '.join(sorted(RUNTIMES))}")
    releases = RUNTIMES[runtime]() if releases is None else releases
    cands, floating, line = spec_filter(runtime, spec, releases)
    if not cands:
        raise LookupError(f"no {runtime} release matches {spec!r}")
    cands = sorted(cands, key=lambda r: _gh.semver(r["version"]), reverse=True)

    def with_age(r):
        if not r:
            return None
        age = round(_gh.age_days(r["date"], at), 1) if r.get("date") else None
        return {**r, "age_days": age}

    def old_enough(r):
        return r.get("date") and _gh.age_days(r["date"], at) >= min_age

    security = next((r for r in cands if r.get("security")), None)
    past = next((r for r in cands if old_enough(r)), None)
    if floating:
        today = cands[0]
        pin = today
        note = (f"{spec} resolves to {today['version']} today, so that version is already in use. Pin it, "
                "never a lower one")
    else:
        exact = re.sub(r"^(go|v)", "", spec.strip().lower())
        want = _gh.semver(exact)
        today = next((r for r in cands if _gh.semver(r["version"]) == want), {"version": exact, "date": None})
        pin = past if past and _gh.semver(past["version"]) > want else today
        note = ("the newest release on the line past the cooldown" if pin is past
                else f"{today['version']} stays: no newer release on the line is past the cooldown")
    inside = bool(pin.get("date")) and not old_enough(pin)
    if inside:
        note += (". It is inside the cooldown: a security release" if pin.get("security") else
                 ". It is inside the cooldown (already merged)")
    newer_security = bool(security) and _gh.semver(security["version"]) > _gh.semver(pin["version"])
    if newer_security:
        note += f". {security['version']} is a newer security release on the line"
    return {"runtime": runtime, "spec": spec, "floating": floating, "line": line,
            "resolves_today": with_age(cands[0]) if floating else with_age(today),
            "newest_past_cooldown": with_age(past), "latest_security": with_age(security),
            "pin": pin["version"], "pin_inside_cooldown": inside, "pin_security": pin.get("security"),
            "newer_security_release": newer_security, "min_age_days": min_age, "note": note}


def print_check(rows, as_json):
    bad = [r for r in rows if r.get("problems")]
    skipped = [r for r in rows if "skip" in r]
    unchecked = [r for r in rows if r.get("not_checked")]
    checked = len(rows) - len(skipped) - len(unchecked)
    if as_json:
        json.dump({"checked": checked, "problems": bad, "skipped": skipped, "not_checked": unchecked},
                  sys.stdout, indent=2)
        print()
    else:
        for r in bad:
            for msg in r["problems"]:
                print(f"{r['file']}:{r['line']}: {r['uses']}: {msg}")
        for r in unchecked:
            print(f"{r['file']}:{r['line']}: {r['uses']}: not checked, {r['not_checked']}")
        for r in skipped:
            print(f"{r['file']}:{r['line']}: {r['uses']}: skipped, {r['skip']}")
        print(f"{checked} pins checked, {len(bad)} with problems, {len(unchecked)} not checked, "
              f"{len(skipped)} skipped.")
    return 1 if bad else (3 if unchecked else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    _gh.add_json(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="check the pins in workflow, action, compose and Dockerfile files")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE)
    _gh.add_json(p, sub=True)
    p = sub.add_parser("resolve", help="find the newest action release that is old enough")
    p.add_argument("action", metavar="OWNER/ACTION")
    p.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE)
    _gh.add_json(p, sub=True)
    p = sub.add_parser("image", help="work with container images")
    _gh.add_json(p, sub=True)
    isub = p.add_subparsers(dest="icmd", required=True)
    q = isub.add_parser("resolve", help="find the newest image digest that is old enough")
    q.add_argument("image", metavar="IMAGE[:TAG]")
    q.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE)
    _gh.add_json(q, sub=True)
    q = isub.add_parser("newest", help="find the newest exact version tag with an old enough digest")
    q.add_argument("image", metavar="IMAGE")
    q.add_argument("--variant", help="the tag suffix after the version, such as slim or alpine")
    q.add_argument("--line", help="only versions on this line, such as 3.15 or 1")
    q.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE)
    _gh.add_json(q, sub=True)
    p = sub.add_parser("runtime", help="what a runtime version spec resolves to today, and what to pin")
    p.add_argument("name", choices=sorted(RUNTIMES))
    p.add_argument("spec", help="the version spec, such as 24, 24.x, lts/*, 3.15, 3.15.0, 1.25 or stable")
    p.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE)
    _gh.add_json(p, sub=True)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "check":
            return print_check(check(a.files, a.min_age_days), a.json)
        if a.cmd == "runtime":
            try:
                res = resolve_runtime(a.name, a.spec, a.min_age_days)
            except (ValueError, LookupError) as e:
                print(f"pins.py runtime: {e}", file=sys.stderr)
                return 2
            if a.json:
                json.dump(res, sys.stdout, indent=2)
                print()
            else:
                today = res["resolves_today"]
                print(f"pin {a.name} {res['pin']}")
                print(f"{a.spec} resolves to {today['version']} today ({(today.get('date') or 'undated')[:10]})")
                print(res["note"] + ".")
            return 0
        if a.cmd == "image":
            res = (newest_image(a.image, a.variant, a.line, a.min_age_days) if a.icmd == "newest"
                   else resolve_image(a.image, a.min_age_days))
            if a.json:
                json.dump(res, sys.stdout, indent=2)
                print()
            elif res.get("digest"):
                print(res["pin"])
                print(f"published {res['published_at']} ({res['source']})")
            else:
                print(f"{a.image}: {res['reason']}", file=sys.stderr)
            return 0 if res.get("digest") else 1
        res = resolve(a.action, a.min_age_days)
        if not res or not res.get("tag"):
            msg = f"{a.action}: no release is at least {a.min_age_days:g} days old."
            if res and res.get("undated"):
                msg += f" Sadr cannot date these lightweight tags: {', '.join(res['undated'][:10])}."
            print(msg, file=sys.stderr)
            return 1
        if a.json:
            json.dump(res, sys.stdout, indent=2)
            print()
        else:
            print(f"uses: {a.action}@{res['sha']} # {res['tag']}")
            print(f"published {res['published_at']} ({res['source']})")
        return 0
    except _gh.GhError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
