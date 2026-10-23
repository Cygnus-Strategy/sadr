#!/usr/bin/env python3
"""Publish a local branch to GitHub as Verified commits made through the API.

Sadr never runs `git push`. It builds commits locally with plain `git
commit`, then this script recreates each unpublished commit on GitHub with
the App token, so GitHub signs them and marks them Verified.

The script compares the local branch with the remote one:

- When the remote branch does not exist, it creates the ref at the base:
  --base, or else the remote default branch's head. The base must be an
  ancestor of the local branch.
- When it exists, its tip must be an ancestor of the local branch. If
  another run moved it, the script stops and publishes nothing (exit 4).

Then it recreates each local commit after that point, oldest first, with
the same message and the same tree. GitHub signs a commit made through
the API, and marks it Verified, only when the commit carries the App's
own identity. So the commits name no author and no committer: GitHub
shows the App's bot login, such as sadrbot[bot], as the author.

- graphql: createCommitOnBranch with fileChanges (base64 additions and
  deletions) and expectedHeadOid. auto uses it whenever it can. It cannot express
  a merge commit, a symlink, a submodule, an executable file, or a mode
  change, because GitHub writes every file it adds as a regular file.
- gitdata: the git data API (blobs, a tree on the parent's tree, a commit,
  then a fast-forward of the ref). It keeps file modes, symlinks,
  submodules and merge commits, and checks that each tree GitHub builds
  has the same SHA as the local tree. It is only the fallback for the
  changes that graphql cannot express.

--method auto (the default) uses graphql, and gitdata only when a commit
to publish has a change that graphql cannot express. The result lists
those reasons. --method graphql exits 3 with the reasons instead.

Each result lists the new commit SHAs and whether GitHub reports them as
verified. The script reads verification.verified back for each gitdata
commit, and createCommitOnBranch returns it for each graphql commit.
When GitHub did not verify one, the script prints a warning and still
exits 0. After publishing, the script fetches the
branch and moves the local branch to the new remote tip (same tree, new
SHAs), so the next call starts from there. --no-sync skips that.

--dry-run reads the remote branch and prints the plan, and writes nothing.

Exit codes: 0 published (or nothing to publish), 2 error, 3 a change
cannot be expressed with --method graphql, 4 the remote branch moved.

Examples:
  commit.py --repo acme/app --branch sadr/upkeep
  commit.py --repo acme/app --branch sadr/facts --dry-run --json
  commit.py --repo acme/app --branch sadr/issue-12 --dir ~/work/app --method gitdata

Needs `git`, and a `gh` CLI that uses the App token (GH_TOKEN). The script
never prints the token. Standard library only.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

import _gh

GRAPHQL_LIMIT = 40 * 1024 * 1024
REGULAR = "100644"
EXECUTABLE = "100755"
SYMLINK = "120000"
GITLINK = "160000"
NULL = "0" * 40
CREATE = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid signature { isValid state } }
  }
}"""
SIGNATURE = """
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    object(oid: $oid) { ... on Commit { oid parents(first: 2) { nodes { oid } } tree { oid } } }
  }
}"""


class PublishError(RuntimeError):
    code = 2


class Unexpressible(PublishError):
    code = 3


class Moved(PublishError):
    code = 4


def git(args, cwd, binary=False):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=not binary)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace") if binary else r.stderr
        raise PublishError(f"git {' '.join(args)}: {err.strip()}")
    return r.stdout


def gh_input(args, body):
    """Run `gh api ARGS --input FILE` with BODY as JSON. Return parsed JSON.

    The body goes through a private temp file, never the command line.
    """
    fd, path = tempfile.mkstemp(prefix="sadr-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        out = _gh.run(["api", *args, "--input", path], retries=0)
    finally:
        os.unlink(path)
    return json.loads(out or "null")


def remote_tip(repo, branch):
    ref = _gh.api(f"repos/{repo}/git/ref/heads/{branch}")
    return ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None


def default_tip(repo):
    info = _gh.api(f"repos/{repo}") or {}
    branch = info.get("default_branch")
    if not branch:
        raise PublishError(f"cannot read the default branch of {repo}")
    sha = remote_tip(repo, branch)
    if not sha:
        raise PublishError(f"cannot read the head of {repo}@{branch}")
    return sha


def local_commits(cwd, anchor, head):
    """[(sha, [parent shas])] after ANCHOR up to HEAD, oldest first.

    Commits that a remote-tracking ref of the clone already contains, such
    as those of origin/main after a merge, are on GitHub already and stay
    out.
    """
    out = git(["rev-list", "--reverse", "--topo-order", "--parents", head, "--not", anchor, "--remotes"], cwd)
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            rows.append((parts[0], parts[1:]))
    return rows


def changes(cwd, parent, commit):
    """The file changes of COMMIT against PARENT: dicts with path, old_mode, new_mode, sha, status."""
    raw = git(["diff-tree", "-r", "-z", "--no-renames", "--raw", "--no-commit-id", parent, commit], cwd)
    parts = raw.split("\0")
    out, i = [], 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        old_mode, new_mode, old_sha, new_sha, status = meta[1:].split()[:5]
        out.append({"path": parts[i + 1], "old_mode": old_mode, "new_mode": new_mode, "old_sha": old_sha,
                    "sha": new_sha, "status": status[0]})
        i += 2
    return out


def graphql_problems(chs):
    """Reasons why createCommitOnBranch cannot express these changes. Empty when it can."""
    why = []
    for c in chs:
        modes = {c["old_mode"], c["new_mode"]} - {"000000"}
        if SYMLINK in modes:
            why.append(f"{c['path']}: a symlink")
        elif GITLINK in modes:
            why.append(f"{c['path']}: a submodule")
        elif c["status"] != "D" and c["new_mode"] == EXECUTABLE:
            why.append(f"{c['path']}: an executable file (mode 100755)")
        elif c["status"] == "M" and c["old_mode"] != c["new_mode"]:
            why.append(f"{c['path']}: a mode change {c['old_mode']} to {c['new_mode']}")
    return why


def message_of(cwd, commit):
    return git(["log", "-1", "--format=%B", commit], cwd).rstrip("\n")


def blob(cwd, sha):
    return git(["cat-file", "blob", sha], cwd, binary=True)


def plan(cwd, anchor, head, method):
    """Return the publish plan: method, commits and the reasons for the method."""
    commits = local_commits(cwd, anchor, head)
    rows, reasons = [], []
    for sha, parents in commits:
        chs = changes(cwd, parents[0], sha) if parents else []
        why = graphql_problems(chs)
        if len(parents) > 1:
            why.insert(0, f"{sha[:7]} is a merge commit")
        size = sum(len(blob(cwd, c["sha"])) for c in chs if c["status"] != "D" and c["new_mode"] != GITLINK)
        if size * 4 // 3 > GRAPHQL_LIMIT:
            why.append(f"{sha[:7]} adds {size // (1024 * 1024)} MB, too much for one GraphQL request")
        reasons += why
        rows.append({"local": sha, "parents": parents, "tree": git(["rev-parse", f"{sha}^{{tree}}"], cwd).strip(),
                     "message": message_of(cwd, sha), "changes": chs, "graphql_problems": why})
    if method == "graphql" and reasons:
        raise Unexpressible("createCommitOnBranch cannot express: " + "; ".join(reasons)
                            + ". Use --method auto or gitdata.")
    if method == "auto":
        method = "gitdata" if reasons else "graphql"
    return {"method": method, "commits": rows, "reasons": reasons}


def publish_graphql(repo, branch, row, expected, cwd):
    """Create one commit with createCommitOnBranch. Return (new sha, verified)."""
    additions, deletions = [], []
    for c in row["changes"]:
        if c["status"] == "D":
            deletions.append({"path": c["path"]})
        else:
            additions.append({"path": c["path"], "contents": base64.b64encode(blob(cwd, c["sha"])).decode()})
    headline, _, body = row["message"].partition("\n")
    msg = {"headline": headline.strip() or "sadr: update"}
    if body.strip():
        msg["body"] = body.strip("\n")
    payload = {"query": CREATE, "variables": {"input": {
        "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
        "message": msg, "expectedHeadOid": expected,
        "fileChanges": {"additions": additions, "deletions": deletions}}}}
    try:
        data = gh_input(["graphql"], payload)
    except _gh.GhError as e:
        # A retry after a lost answer would fail on expectedHeadOid. Check
        # whether the first try landed: the tip's parent is EXPECTED and its
        # tree is ours.
        landed = landed_commit(repo, branch, expected, row["tree"])
        if landed:
            return landed, None
        raise PublishError(f"createCommitOnBranch: {e}") from None
    if data.get("errors"):
        raise PublishError(f"createCommitOnBranch: {data['errors'][0].get('message')}")
    commit = ((data.get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}
    if not commit.get("oid"):
        raise PublishError("createCommitOnBranch returned no commit")
    sig = commit.get("signature") or {}
    return commit["oid"], sig.get("isValid")


def landed_commit(repo, branch, parent, tree):
    tip = remote_tip(repo, branch)
    if not tip or tip == parent:
        return None
    owner, name = repo.split("/", 1)
    try:
        obj = _gh.graphql(SIGNATURE, owner=owner, name=name, oid=tip)["repository"]["object"] or {}
    except (_gh.GhError, KeyError, TypeError):
        return None
    parents = [p["oid"] for p in (obj.get("parents") or {}).get("nodes") or []]
    if parents == [parent] and (obj.get("tree") or {}).get("oid") == tree:
        return tip
    return None


def publish_gitdata(repo, row, mapped, cwd):
    """Create one commit through the git data API. Return (new sha, verified).

    The commit names no author and no committer. GitHub then uses the
    App's identity and signs the commit. An explicit author makes GitHub
    leave the commit unsigned.
    """
    parents = [mapped.get(p, p) for p in row["parents"]]
    base_tree = git(["rev-parse", f"{row['parents'][0]}^{{tree}}"], cwd).strip()
    entries = []
    for c in row["changes"]:
        if c["status"] == "D":
            entries.append({"path": c["path"], "mode": c["old_mode"], "type": "blob", "sha": None})
        elif c["new_mode"] == GITLINK:
            entries.append({"path": c["path"], "mode": GITLINK, "type": "commit", "sha": c["sha"]})
        else:
            res = gh_input(["-X", "POST", f"repos/{repo}/git/blobs"],
                           {"content": base64.b64encode(blob(cwd, c["sha"])).decode(), "encoding": "base64"})
            if (res or {}).get("sha") != c["sha"]:
                raise PublishError(f"{c['path']}: GitHub stored blob {(res or {}).get('sha')}, expected {c['sha']}")
            entries.append({"path": c["path"], "mode": c["new_mode"], "type": "blob", "sha": c["sha"]})
    tree = gh_input(["-X", "POST", f"repos/{repo}/git/trees"], {"base_tree": base_tree, "tree": entries})
    if (tree or {}).get("sha") != row["tree"]:
        raise PublishError(f"{row['local'][:7]}: GitHub built tree {(tree or {}).get('sha')}, expected {row['tree']}")
    commit = gh_input(["-X", "POST", f"repos/{repo}/git/commits"],
                      {"message": row["message"] + "\n", "tree": row["tree"], "parents": parents})
    if not (commit or {}).get("sha"):
        raise PublishError("GitHub returned no commit")
    return commit["sha"], read_verified(repo, commit["sha"])


def read_verified(repo, sha):
    """Read verification.verified of the commit SHA back from GitHub. None when GitHub does not say."""
    try:
        got = _gh.api(f"repos/{repo}/git/commits/{sha}")
    except _gh.GhError:
        return None
    v = ((got or {}).get("verification") or {}).get("verified") if isinstance(got, dict) else None
    return v if isinstance(v, bool) else None


def move_ref(repo, branch, sha, exists):
    if exists:
        gh_input(["-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}"], {"sha": sha, "force": False})
    else:
        gh_input(["-X", "POST", f"repos/{repo}/git/refs"], {"ref": f"refs/heads/{branch}", "sha": sha})


def sync_local(cwd, branch, sha):
    """Fetch BRANCH and point the local branch at SHA, keeping the work tree."""
    git(["fetch", "--quiet", "origin", f"refs/heads/{branch}"], cwd)
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    if current == branch:
        git(["reset", "--soft", sha], cwd)
    else:
        git(["update-ref", f"refs/heads/{branch}", sha], cwd)


def publish(repo, branch, cwd=".", base=None, method="auto", dry_run=False, sync=True, local=None):
    """Publish the local BRANCH (or LOCAL ref) to REPO. Return a result dict."""
    local = local or branch
    head = git(["rev-parse", "--verify", f"{local}^{{commit}}"], cwd).strip()
    tip = remote_tip(repo, branch)
    anchor = tip or base or default_tip(repo)
    if subprocess.run(["git", "cat-file", "-e", f"{anchor}^{{commit}}"], cwd=cwd, capture_output=True).returncode:
        raise Moved(f"the local clone does not have {anchor[:7]}, the remote {'tip of ' + branch if tip else 'base'}. "
                    f"Fetch, and check whether another run moved the branch")
    if subprocess.run(["git", "merge-base", "--is-ancestor", anchor, head], cwd=cwd).returncode:
        raise Moved(f"the remote {'branch ' + branch + ' at' if tip else 'base'} {anchor[:7]} is not an ancestor "
                    f"of the local {local} at {head[:7]}. Another run may have moved it. Publish nothing")
    p = plan(cwd, anchor, head, method)
    ours = {c["local"] for c in p["commits"]}
    for c in p["commits"]:
        for parent in c["parents"]:
            if parent not in ours and parent != anchor and not _gh.api(f"repos/{repo}/git/commits/{parent}"):
                raise PublishError(f"{c['local'][:7]} has parent {parent[:7]}, which is not on GitHub. "
                                   f"Publish that branch first, or rebuild without it")
    result = {"repo": repo, "branch": branch, "remote_tip": tip, "base": None if tip else anchor,
              "created_ref": False, "method": p["method"], "reasons": p["reasons"], "dry_run": dry_run,
              "commits": [{"local": c["local"], "message": c["message"].split("\n", 1)[0],
                           "files": len(c["changes"])} for c in p["commits"]]}
    if dry_run or not p["commits"]:
        result["head"] = tip
        return result
    current, mapped = tip or anchor, {}
    if p["method"] == "graphql":
        if not tip:
            move_ref(repo, branch, anchor, exists=False)
            result["created_ref"] = True
        for row, out in zip(p["commits"], result["commits"]):
            sha, verified = publish_graphql(repo, branch, row, current, cwd)
            out.update(remote=sha, verified=verified)
            current = mapped[row["local"]] = sha
    else:
        for row, out in zip(p["commits"], result["commits"]):
            sha, verified = publish_gitdata(repo, row, mapped, cwd)
            out.update(remote=sha, verified=verified)
            current = mapped[row["local"]] = sha
        move_ref(repo, branch, current, exists=bool(tip))
        result["created_ref"] = not tip
    result["head"] = current
    result["unverified"] = [c["remote"] for c in result["commits"] if c.get("verified") is False]
    if sync and local == branch:
        try:
            sync_local(cwd, branch, current)
            result["synced"] = True
        except PublishError as e:
            result["synced"] = False
            result["sync_error"] = str(e)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument("--branch", required=True, help="the remote branch to publish to, such as sadr/upkeep")
    ap.add_argument("--local", help="the local branch or commit to publish (default: the same name as --branch)")
    ap.add_argument("--dir", default=".", help="the local clone (default: the current folder)")
    ap.add_argument("--base", metavar="SHA", help="where a new remote branch starts (default: the default branch head)")
    ap.add_argument("--method", choices=["auto", "graphql", "gitdata"], default="auto",
                    help="auto (default): graphql, or gitdata when a change needs it")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    ap.add_argument("--no-sync", action="store_true", help="leave the local branch where it is after publishing")
    ap.add_argument("--json", action="store_true", help="print JSON")
    a = ap.parse_args(argv)
    if "/" not in a.repo:
        ap.error("--repo takes OWNER/NAME")
    if a.branch in ("main", "master") or not a.branch.startswith("sadr/"):
        ap.error("Sadr publishes only sadr/ branches")
    try:
        res = publish(a.repo, a.branch, a.dir, a.base, a.method, a.dry_run, not a.no_sync, a.local)
    except PublishError as e:
        print(f"commit.py: {e}", file=sys.stderr)
        return e.code
    except _gh.GhError as e:
        print(f"commit.py: {e}", file=sys.stderr)
        return 2
    if a.json:
        json.dump(res, sys.stdout, indent=2)
        print()
    else:
        verb = "would publish" if a.dry_run else "published"
        print(f"{a.repo} {a.branch}: {verb} {len(res['commits'])} commits with {res['method']}")
        for c in res["commits"]:
            v = {True: "verified", False: "NOT verified", None: "verification unknown"}[c.get("verified")]
            print(f"  {c['local'][:7]} -> {(c.get('remote') or '-')[:7]} {v if c.get('remote') else ''} "
                  f"{c['message']}".rstrip())
        if res.get("sync_error"):
            print(f"  local branch not moved: {res['sync_error']}", file=sys.stderr)
    if res.get("unverified"):
        print(f"commit.py: WARNING: GitHub did not verify {len(res['unverified'])} commits on {a.repo} {a.branch}: "
              f"{', '.join(s[:7] for s in res['unverified'])} ({res['method']}). The commits are published. "
              f"List them under Needs you. GitHub signs API commits only under the App's own identity: "
              f"check that GH_TOKEN is the App's installation token.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
