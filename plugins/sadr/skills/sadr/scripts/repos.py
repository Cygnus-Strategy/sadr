#!/usr/bin/env python3
"""List the repos in Sadr's scope, fresh on every run.

Sadr takes every repo of the organizations and users that it is given.
It leaves out forks, archived repos and empty repos. For each repo the
script reports:

- the default branch and the commit at its head
- whether the root AGENTS.md exists there, and whether it has a ## Sadr
  section
- the text of that ## Sadr section (sadr_section), or null without one
- the owner's settings: whether OWNER/.github has a root AGENTS.md on its
  default branch (owner_agents_md: true, false when the repo or the file
  does not exist, null when the token cannot read it, with the reason in
  owner_agents_md_error), and the text of its ## Sadr section
  (owner_sadr_section, null without one). The script reads it once per
  owner. A repo's Settings come from sadr_section first, then from
  owner_sadr_section.
- whether the repo opted out ("Sadr: off" in the ## Sadr section)
- the open Sadr PRs: the facts PR (branch sadr/facts), the upkeep PR
  (branch sadr/upkeep) and the issue PRs (branches sadr/issue-<N> and
  sadr/issue-<N>-<part>)
- whether a run claimed the repo: a push to a sadr/ branch in the last hour
- the open PRs of other automation: PRs that a bot opened, and PRs from
  branches that start with dependabot/, renovate/ or an --other-prefix.
  Each row lists every file the PR touches (files) and the manifests,
  lockfiles and workflows among them (manifest_files). Sadr leaves only
  those files alone, and never for a cooldown setting or a security fix.
- the related repos: OWNER/NAME names in the ## Sadr section that exist
  and that the token can read. Names that the token cannot read are listed
  as unreachable.
- the inferred related repos: sibling repos of the same owner that a
  checked-in build file names, such as Package.swift, an Xcode project,
  package.json, go.mod, go.work, pyproject.toml, Cargo.toml,
  settings.gradle or .gitmodules. A sibling is a relative path that leaves
  the repo ("../field-ui"), or a GitHub URL of the same owner. Each row
  names the file, line and evidence. Only repos that exist, that the token
  can read, and that the ## Sadr section does not already list are
  included; the others go to inferred_related_repos_unreachable.
- an error, when GitHub failed for this repo. The other repos still get
  their rows.

Examples:
  repos.py --org acme
  repos.py --user octocat --json
  repos.py --repo acme/app --repo acme/tools
  repos.py --org acme --other-prefix factory/   (a tool that Sadr replaces)

Needs an authenticated `gh` CLI. Standard library only.
"""
import argparse
import concurrent.futures
import datetime
import json
import re
import sys
import urllib.parse

import _gh

UPKEEP_BRANCH = "sadr/upkeep"
FACTS_BRANCH = "sadr/facts"
FEATURE_BRANCH = re.compile(r"sadr/issue-(\d+)(?:-([\w.-]+))?")
OTHER_BRANCHES = ("dependabot/", "renovate/")
# Checked-in build files that can name a sibling repo.
BUILD_FILE = re.compile(
    r"(^|/)(Package\.swift|project\.pbxproj|[\w.-]+\.xcworkspace/contents\.xcworkspacedata|package\.json|"
    r"pnpm-workspace\.yaml|go\.mod|go\.work|pyproject\.toml|Cargo\.toml|settings\.gradle(\.kts)?|"
    r"build\.gradle(\.kts)?|\.gitmodules|Podfile|Cartfile|composer\.json|Gemfile|requirements[\w.-]*\.txt|"
    r"CMakeLists\.txt|west\.yml|idf_component\.yml|devenv\.yaml|flake\.nix)$")
BUILD_SKIP_DIRS = re.compile(r"(^|/)(node_modules|vendor|third_party|\.build|build|dist|Pods|Carthage|\.git|"
                             r"DerivedData|target|\.venv|venv)/")
MAX_BUILD_FILES = 60
SIBLING = re.compile(r"""(?:^|[\s"'=(:,\[{])((?:\.\./)+)([A-Za-z0-9][A-Za-z0-9._-]{0,99})""")
GITHUB_URL = re.compile(r"(?:github\.com[:/]|\bgithub:)([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?(?=[\s\"'/#@)?,;]|$)")
# Files whose change means a PR competes with Sadr's upkeep.
MANIFEST = re.compile(
    r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|yarn\.lock|"
    r"bun\.lockb?|deno\.jsonc?|deno\.lock|\.npmrc|\.yarnrc\.yml|pyproject\.toml|uv\.lock|poetry\.lock|"
    r"requirements[\w.-]*\.(txt|in)|Pipfile(\.lock)?|setup\.(py|cfg)|go\.mod|go\.sum|go\.work(\.sum)?|"
    r"Cargo\.toml|Cargo\.lock|Package\.swift|Package\.resolved|Gemfile(\.lock)?|composer\.(json|lock)|"
    r"[\w.-]*\.py\.lock|Dockerfile[\w.-]*|[\w.-]*\.Dockerfile|[\w.-]*compose[\w.-]*\.ya?ml|"
    r"\.node-version|\.nvmrc|\.python-version|\.tool-versions|mise\.toml|rust-toolchain(\.toml)?|"
    r"devenv\.(nix|yaml|lock)|flake\.(nix|lock)|action\.ya?ml|renovate\.json5?|dependabot\.ya?ml)$"
    r"|(^|/)\.github/workflows/")
SLUG = re.compile(r"(?<![\w./~@-])(?:https://github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})"
                  r"(?![\w/-])")
COMMON_DIRS = {"src", "lib", "libs", "app", "apps", "bin", "cmd", "pkg", "internal", "docs", "doc", "test", "tests",
               "spec", "scripts", "script", "tools", "public", "static", "assets", "build", "dist", "web", "server",
               "client", "api", "worker", "workers", "packages", "templates", "migrations", "config", "data",
               "examples", "vendor", "node_modules", "workflows", "main", "master"}
NOT_A_REPO = re.compile(r"\.(md|py|ya?ml|json|toml|lock|txt|js|ts|go|rs|swift|sh|nix|html|css)$", re.I)
CLAIM_WINDOW = datetime.timedelta(hours=1)
REPO_FIELDS = "nameWithOwner,isFork,isArchived,isEmpty,defaultBranchRef,pushedAt"


def sadr_section(text):
    """Return the lines of the '## Sadr' section of an AGENTS.md text.

    The section starts at a level-two heading named exactly 'Sadr'. It ends
    at the next level-one or level-two heading. Lines in code fences do not
    count as headings.
    """
    lines, inside, fence = [], False, False
    for line in (text or "").splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        if not fence and re.match(r"#{1,2}(\s|$)", line):
            inside = re.fullmatch(r"##\s+Sadr\s*#*\s*", line) is not None
            continue
        if inside:
            lines.append(line)
    return lines


def section_text(text):
    """The text of the '## Sadr' section of an AGENTS.md text, or None without one.

    A section with a heading and nothing under it gives ''.
    """
    if not has_section(text):
        return None
    return "\n".join(sadr_section(text)).strip("\n")


def owner_agents(owner):
    """Read the root AGENTS.md of OWNER/.github on its default branch.

    Return {owner_agents_md, owner_sadr_section, owner_agents_md_error}.
    owner_agents_md is True when the file exists, False when GitHub answers
    404 (no .github repo, or no AGENTS.md in it), and None when GitHub
    refuses or fails, with the reason in owner_agents_md_error.
    """
    try:
        text = _gh.api(f"repos/{owner}/.github/contents/AGENTS.md", raw=True)
    except _gh.GhError as e:
        return {"owner_agents_md": None, "owner_sadr_section": None,
                "owner_agents_md_error": f"cannot read {owner}/.github AGENTS.md: {e}"}
    return {"owner_agents_md": text is not None, "owner_sadr_section": section_text(text),
            "owner_agents_md_error": None}


def has_section(text):
    """True when an AGENTS.md text has a '## Sadr' heading outside code fences."""
    fence = False
    for line in (text or "").splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        elif not fence and re.fullmatch(r"##\s+Sadr\s*#*\s*", line):
            return True
    return False


def slugs(lines):
    """OWNER/NAME names in the lines of a ## Sadr section, in order, without duplicates.

    Return (name, linked) pairs. linked is true for a github.com URL. A path
    such as docs/agents-md.md or ../field-ui is not a name: the script skips
    text after a slash or a dot, text followed by a slash, and names that
    end in a file extension.
    """
    out, seen = [], set()
    for line in lines:
        for m in SLUG.finditer(line):
            owner, name = m.group(1), m.group(2).rstrip(".")
            if NOT_A_REPO.search(name) or (name.startswith(".") and name != ".github"):
                continue
            linked = m.group(0).startswith("https://")
            slug = f"{owner}/{name}"
            if slug.lower() not in seen:
                seen.add(slug.lower())
                out.append((slug, linked))
    return out


def related(text, repo, owners=()):
    """Related repos named in the ## Sadr section: (readable, unreachable).

    A name that exists and that the token can read is related. A name that
    the token cannot read is unreachable when it is a github.com link or
    its owner is in scope (the repo's owner or OWNERS). Other names that do
    not resolve are text, not repos, and the script drops them. So is a
    path such as worker/src outside the scope, without a lookup.
    """
    scope = {repo.split("/")[0].lower(), *(o.lower() for o in owners)}
    found, unreachable = [], []
    for slug, linked in slugs(sadr_section(text)):
        in_scope = slug.split("/")[0].lower() in scope
        if slug.lower() == repo.lower() or (not linked and not in_scope and slug.split("/")[1].lower() in COMMON_DIRS):
            continue
        try:
            info = _gh.api(f"repos/{slug}")
        except _gh.GhError:
            info = None
        if isinstance(info, dict) and info.get("full_name"):
            found.append(info["full_name"])
        elif linked or in_scope:
            unreachable.append(slug)
    return found, unreachable


def opt_out(text):
    """Return the opt-out line of an AGENTS.md text, or None.

    A repo opts out with a line that starts "Sadr: off" in its ## Sadr
    section. A list marker or bold text before it is fine. A sentence that
    only mentions the phrase does not count.
    """
    for line in sadr_section(text):
        if re.match(r"\s*(?:[-*+]\s+)?(?:\*\*|__)?sadr:\s*off\b", line, re.I):
            return line.strip().lstrip("-* ").strip()
    return None


def classify(pulls, repo, with_facts=False):
    """Split open PRs into the upkeep PR and the issue PRs of REPO.

    With WITH_FACTS, return (facts PR, upkeep PR, issue PRs).

    Only PRs from a branch in REPO itself count. A fork's PR from a branch
    named sadr/upkeep is not Sadr's.
    """
    upkeep, features, facts = None, [], None
    for p in pulls:
        head = p.get("head") or {}
        if ((head.get("repo") or {}).get("full_name") or "").lower() != repo.lower():
            continue
        ref = head.get("ref") or ""
        row = {"number": p["number"], "url": p["html_url"], "branch": ref,
               "title": p.get("title", ""), "updated_at": p.get("updated_at")}
        m = FEATURE_BRANCH.fullmatch(ref)
        if ref == UPKEEP_BRANCH:
            upkeep = row
        elif ref == FACTS_BRANCH:
            facts = row
        elif m:
            row["issue"] = int(m.group(1))
            if m.group(2):
                row["part"] = m.group(2)
            features.append(row)
    features.sort(key=lambda r: r["number"])
    if with_facts:
        return facts, upkeep, features
    return upkeep, features


def is_bot(user):
    user = user or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def other_automation(pulls, repo, prefixes=()):
    """Open PRs of other automation, with every file each one touches.

    A PR counts when a bot opened it, or when its branch starts with
    dependabot/, renovate/ or one of PREFIXES. Sadr's own branches never
    count. "files" lists every file of the PR, "manifest_files" the
    manifests, lockfiles and workflows among them.
    """
    starts = OTHER_BRANCHES + tuple(prefixes)
    out = []
    for p in pulls:
        ref = (p.get("head") or {}).get("ref") or ""
        if ref.startswith("sadr/") or not (ref.startswith(starts) or is_bot(p.get("user"))):
            continue
        files = _gh.api(f"repos/{repo}/pulls/{p['number']}/files?per_page=100", paginate=True) or []
        names = [f["filename"] for f in files if f.get("filename")]
        out.append({"number": p["number"], "url": p["html_url"], "branch": ref, "title": p.get("title", ""),
                    "author": (p.get("user") or {}).get("login"), "created_at": p.get("created_at"),
                    "files": names, "manifest_files": [n for n in names if MANIFEST.search(n)]})
    out.sort(key=lambda r: r["number"])
    return out


def sibling_refs(path, text):
    """(sibling name, line, evidence) for each relative path in a build file that leaves the repo.

    PATH is the build file's path in the repo. A relative path resolves
    from the file's folder; in an Xcode project or workspace, from the
    folder that holds the .xcodeproj or .xcworkspace.
    """
    parts = path.split("/")[:-1]
    if path.endswith((".pbxproj", "contents.xcworkspacedata")):
        parts = parts[:-1]
    depth = len(parts)
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in SIBLING.finditer(line):
            ups = m.group(1).count("../")
            if ups == depth + 1:
                name = m.group(2).rstrip(".")
                if name.endswith(".git"):
                    name = name[:-4]
                out.append((name, n, line.strip()[:200]))
    return out


def url_refs(owner, text):
    """(OWNER/NAME, line, evidence) for each GitHub URL of OWNER in a build file."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in GITHUB_URL.finditer(line):
            if m.group(1).lower() == owner.lower():
                out.append((f"{m.group(1)}/{m.group(2)}", n, line.strip()[:200]))
    return out


def build_files(repo, branch):
    """Paths of checked-in build files on BRANCH, outside vendored and build folders."""
    tree = _gh.api(f"repos/{repo}/git/trees/{branch}?recursive=1") or {}
    paths = [t["path"] for t in tree.get("tree") or [] if t.get("type") == "blob"
             and BUILD_FILE.search(t.get("path") or "") and not BUILD_SKIP_DIRS.search(t["path"])]
    paths.sort(key=lambda p: (p.count("/"), p))
    return paths[:MAX_BUILD_FILES]


def inferred_related(repo, branch, declared=(), files=None, fetch=None):
    """Sibling repos that checked-in build files name: (found, unreachable).

    Each row has repo, file, line, evidence and kind ("path" for a
    relative path, "url" for a GitHub URL). Only repos of REPO's owner
    count, and not REPO itself or a repo in DECLARED.
    """
    owner = repo.split("/")[0]
    skip = {repo.lower(), *(d.lower() for d in declared)}
    files = build_files(repo, branch) if files is None else files
    fetch = fetch or (lambda path: _gh.api(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={branch}",
                                           raw=True))
    candidates = {}
    for path in files:
        text = fetch(path)
        if not text:
            continue
        refs = [(f"{owner}/{name}", n, ev, "path") for name, n, ev in sibling_refs(path, text)]
        refs += [(slug, n, ev, "url") for slug, n, ev in url_refs(owner, text)]
        for slug, n, ev, kind in refs:
            if slug.lower() in skip:
                continue
            candidates.setdefault(slug.lower(), {"repo": slug, "file": path, "line": n, "evidence": ev,
                                                 "kind": kind})
    found, unreachable = [], []
    for row in candidates.values():
        try:
            info = _gh.api(f"repos/{row['repo']}")
        except _gh.GhError:
            info = None
        if isinstance(info, dict) and info.get("full_name"):
            if info["full_name"].lower() in skip:
                continue
            found.append({**row, "repo": info["full_name"]})
        else:
            unreachable.append(row)
    found.sort(key=lambda r: r["repo"].lower())
    unreachable.sort(key=lambda r: r["repo"].lower())
    return found, unreachable


def claims_from_activity(events, at):
    """Pushes to sadr/ branches within CLAIM_WINDOW of AT, newest first."""
    seen = {}
    for e in events or []:
        ref = e.get("ref") or ""
        if not ref.startswith("refs/heads/sadr/") or not e.get("timestamp"):
            continue
        if e.get("activity_type") not in (None, "push", "force_push", "branch_creation"):
            continue
        when = _gh.parse_time(e["timestamp"])
        if at - when <= CLAIM_WINDOW:
            branch = ref[len("refs/heads/"):]
            if branch not in seen or seen[branch] < e["timestamp"]:
                seen[branch] = e["timestamp"]
    return [{"branch": b, "pushed_at": t} for b, t in sorted(seen.items(), key=lambda x: x[1], reverse=True)]


def recent_claims(repo, at):
    """Find recent pushes to sadr/ branches.

    The repository activity API records push times. When it is not
    available, fall back to the commit date at the head of each sadr/ branch.
    """
    try:
        events = _gh.api(f"repos/{repo}/activity?time_period=day&per_page=100")
        return claims_from_activity(events, at)
    except _gh.GhError:
        pass
    out = []
    for ref in _gh.api(f"repos/{repo}/git/matching-refs/heads/sadr/") or []:
        commit = _gh.api(f"repos/{repo}/commits/{ref['object']['sha']}") or {}
        when = (((commit.get("commit") or {}).get("committer")) or {}).get("date")
        if when and at - _gh.parse_time(when) <= CLAIM_WINDOW:
            out.append({"branch": ref["ref"][len("refs/heads/"):], "pushed_at": when})
    return out


def list_owner(owner):
    """All repos of an organization or a user, as `gh repo list` reports them.

    An empty list is suspect: the script asks again, then checks the
    owner's public repo count. Raise SuspectEmpty when the owner has repos.
    """
    for attempt in range(2):
        out = json.loads(_gh.run(["repo", "list", owner, "--limit", "4000", "--json", REPO_FIELDS]) or "[]")
        if out:
            return out
        if not attempt:
            _gh.sleep(5)
    info = _gh.api(f"users/{owner}") or {}
    if info.get("public_repos"):
        raise _gh.SuspectEmpty(f"gh repo list {owner} returned no repos, but {owner} has "
                               f"{info['public_repos']} public repos")
    return out


def view_repo(full):
    out = _gh.run(["repo", "view", full, "--json", REPO_FIELDS])
    return json.loads(out)


def skip_reason(info):
    if info.get("isFork"):
        return "fork"
    if info.get("isArchived"):
        return "archived"
    if info.get("isEmpty"):
        return "empty"
    return None


def head_sha(repo, branch):
    ref = _gh.api(f"repos/{repo}/git/ref/heads/{branch}")
    return ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None


def inspect(info, at, owners=(), other_prefixes=(), owner_info=None):
    """Collect Sadr's facts about one in-scope repo.

    OWNER_INFO is owner_agents() of the repo's owner. Without it, the
    function reads OWNER/.github itself.
    """
    repo = info["nameWithOwner"]
    branch = (info.get("defaultBranchRef") or {}).get("name") or "main"
    agents = _gh.api(f"repos/{repo}/contents/AGENTS.md?ref={branch}", raw=True)
    reason = opt_out(agents) if agents else None
    pulls = _gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) or []
    facts, upkeep, features = classify(pulls, repo, with_facts=True)
    claims = recent_claims(repo, at)
    found, unreachable = related(agents, repo, owners) if agents else ([], [])
    try:
        inferred, inferred_unreachable = inferred_related(repo, branch, found + unreachable)
    except _gh.GhError as e:
        inferred, inferred_unreachable = [], [{"repo": None, "error": f"cannot read the build files: {e}"}]
    return {
        "repo": repo,
        "default_branch": branch,
        "default_head_sha": head_sha(repo, branch),
        "pushed_at": info.get("pushedAt"),
        "has_agents_md": agents is not None,
        "has_sadr_section": has_section(agents),
        "sadr_section": section_text(agents),
        **(owner_info or owner_agents(repo.split("/")[0])),
        "opted_out": reason is not None,
        "opt_out_line": reason,
        "open_sadr_prs": [p for p in (facts, upkeep) if p] + features,
        "facts_pr": facts,
        "upkeep_pr": upkeep,
        "issue_prs": features,
        "feature_prs": features,
        "recently_claimed": bool(claims),
        "claims": claims,
        "other_automation_prs": other_automation(pulls, repo, other_prefixes),
        "related_repos": found,
        "related_repos_unreachable": unreachable,
        "inferred_related_repos": inferred,
        "inferred_related_repos_unreachable": inferred_unreachable,
        "error": None,
    }


def error_row(info, err, owner_info=None):
    """The row of a repo that GitHub failed on."""
    owner = owner_info or {"owner_agents_md": None, "owner_sadr_section": None, "owner_agents_md_error": None}
    return {"repo": info["nameWithOwner"], "default_branch": (info.get("defaultBranchRef") or {}).get("name"),
            "default_head_sha": None, "pushed_at": info.get("pushedAt"), "has_agents_md": None,
            "has_sadr_section": None, "sadr_section": None, **owner, "opted_out": None, "opt_out_line": None, "open_sadr_prs": [],
            "facts_pr": None, "upkeep_pr": None, "issue_prs": [], "feature_prs": [], "recently_claimed": None,
            "claims": [], "other_automation_prs": [], "related_repos": [], "related_repos_unreachable": [],
            "inferred_related_repos": [], "inferred_related_repos_unreachable": [], "error": str(err)}


def safe_inspect(info, at, owners=(), other_prefixes=(), owner_info=None):
    """inspect(), with a GitHub failure kept in the row's error field."""
    try:
        return inspect(info, at, owners, other_prefixes, owner_info)
    except _gh.GhError as e:
        return error_row(info, e, owner_info)


def discover(orgs=(), users=(), repos=(), workers=8, at=None, other_prefixes=()):
    """Return (in-scope rows, skipped rows) for the given owners and repos."""
    at = at or _gh.now()
    infos, skipped, seen = [], [], set()
    candidates = []
    for owner in [*orgs, *users]:
        candidates += list_owner(owner)
    failed = []
    for full in repos:
        try:
            candidates.append(view_repo(full))
        except _gh.GhError as e:
            failed.append(error_row({"nameWithOwner": full}, e))
    for info in candidates:
        name = info["nameWithOwner"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        reason = skip_reason(info)
        if reason:
            skipped.append({"repo": name, "reason": reason})
        else:
            infos.append(info)
    # The owner's AGENTS.md is read once per owner, not once per repo.
    by_owner = {}
    for info in infos:
        owner = info["nameWithOwner"].split("/")[0]
        if owner.lower() not in by_owner:
            by_owner[owner.lower()] = owner_agents(owner)
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        owners = {*orgs, *users, *(r.split("/")[0] for r in repos)}
        rows = list(ex.map(lambda i: safe_inspect(i, at, owners, other_prefixes,
                                                  by_owner[i["nameWithOwner"].split("/")[0].lower()]),
                           infos)) + failed
    rows.sort(key=lambda r: r["repo"].lower())
    skipped.sort(key=lambda r: r["repo"].lower())
    return rows, skipped


def render_md(rows, skipped):
    out = ["| Repo | Branch | AGENTS.md | ## Sadr | Opted out | Facts PR | Upkeep PR | Issue PRs | Other bots | "
           "Claimed | Inferred related |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]

    def yn(v):
        return "?" if v is None else ("yes" if v else "no")

    for r in rows:
        if r.get("error"):
            out.append(f"| {r['repo']} | {r['default_branch'] or '-'} | error: {r['error'][:80]} | | | | | | | | |")
            continue
        up = f"#{r['upkeep_pr']['number']}" if r["upkeep_pr"] else "-"
        facts = f"#{r['facts_pr']['number']}" if r.get("facts_pr") else "-"
        inferred = ", ".join(x["repo"] for x in r.get("inferred_related_repos") or []) or "-"
        feats = ", ".join(f"#{p['number']}" for p in r["feature_prs"]) or "-"
        others = ", ".join(f"#{p['number']}" for p in r["other_automation_prs"]) or "-"
        out.append(f"| {r['repo']} | {r['default_branch']} | {yn(r['has_agents_md'])} | {yn(r['has_sadr_section'])} | "
                   f"{yn(r['opted_out'])} | {facts} | {up} | {feats} | {others} | {yn(r['recently_claimed'])} | {inferred} |")
    out.append("")
    errors = sum(bool(r.get("error")) for r in rows)
    out.append(f"{len(rows)} repos in scope, {sum(bool(r['opted_out']) for r in rows)} opted out, "
               f"{len(skipped)} skipped (forks, archived or empty)" + (f", {errors} with errors." if errors else "."))
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--org", action="append", default=[], help="an organization to list (repeat for more)")
    ap.add_argument("--user", action="append", default=[], help="a user whose repos to list (repeat for more)")
    ap.add_argument("--repo", action="append", default=[], help="one repo as OWNER/NAME (repeat for more)")
    ap.add_argument("--other-prefix", action="append", default=[], metavar="PREFIX",
                    help="also count PRs from branches that start with PREFIX as other automation, such as a "
                         "tool that Sadr replaces (repeat for more)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a Markdown table")
    a = ap.parse_args(argv)
    if not (a.org or a.user or a.repo):
        ap.error("pass at least one --org, --user or --repo")
    try:
        rows, skipped = discover(a.org, a.user, a.repo, other_prefixes=a.other_prefix)
    except _gh.GhError as e:
        sys.exit(str(e))
    for r in rows:
        if r.get("error"):
            print(f"repos.py: {r['repo']}: {r['error']}", file=sys.stderr)
    if a.json:
        json.dump({"repos": rows, "skipped": skipped}, sys.stdout, indent=2)
        print()
    else:
        sys.stdout.write(render_md(rows, skipped))


if __name__ == "__main__":
    main()
