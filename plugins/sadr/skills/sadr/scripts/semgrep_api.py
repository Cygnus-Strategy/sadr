#!/usr/bin/env python3
"""Read Semgrep findings, scans and lockfiles, and record triage, through the Semgrep web API.

The token comes only from the SEMGREP_APP_TOKEN environment variable. It
needs "Web API" access. A CI-only token gets HTTP 404 from every call here.
The script never prints or writes the token.

The deployment (the Semgrep organization) comes from --deployment, then
from SEMGREP_DEPLOYMENT. Without either, the script uses the token's only
deployment. It stops when the token reaches more than one.

Subcommands:
  deployments   list the deployments that the token reaches
  projects      list the projects, the commit of their latest scan, and
                whether that scan is stale
  findings      list findings, optionally only the untriaged ones
  summary       count findings per repo by triage state, one row per repo
  lockfiles     list the lockfiles that Semgrep parsed, with dependency counts
  triage        record a triage decision on findings
  baseline      read the findings of a local `semgrep ci --dry-run` from
                its log, or run that scan first with --run

`findings` and `summary` read all three types (sast, sca and secrets) and
tag each row with its `type`. They read every status that is not closed:
open, fixing and reviewing. They read only the primary branch of each
project, so a finding on refs/pull/N/merge stays out. Pass --ref for
another branch, or --all-refs for every ref.

A finding's `status` is its scan state, and a triage decision changes it
to fixing, reviewing or ignored. A fix that a scan of the primary branch
sees changes it to fixed. Sadr counts a finding as untriaged when its
triage_state is empty, untriaged or reopened.

A scan is stale when its commit is not the head of the default branch.
`projects` reads the head through `gh`, or takes it from --head. It also
prints touch_trigger: true only when the scan is stale, no scan of the
head is running, queued or failed, and the head commit is older than
--grace-hours (default 6). Only then is the scan trigger (the Semgrep
workflow or the app's push hook) the likely cause, and only then should
Sadr change it. touch_trigger is null when the script cannot tell.

`baseline` reads the log of `semgrep ci --dry-run`. The log holds the
"findings and ignores" blob that the scan would have sent, with every
finding, the non-blocking (monitor mode) ones included. The file that
--json-output writes leaves those out, so the log is the source; a
--json-output file given with --json-file is joined in. Each finding gets
type (sast, sca or secrets), rule, path, line, blocking, and for SCA the
package, version, lockfile, reachability, advisory ids and fixed
versions. Findings that a nosemgrep comment ignores are listed with
ignored true. With --api-json FILE (the output of
`findings --status all --json`) or --repo, each finding is matched with
the platform finding by rule and path (SAST, secrets) or by package,
version and advisory (SCA), and gets api_id, api_status and
api_triage_state. A log with no findings blob, or a scan with a fatal
error, is not a baseline: the script exits 2 and says why. --run runs
`semgrep ci --dry-run` in --dir first and writes its log to --log. It
exits 2 when semgrep is not on PATH, and never installs it.

Examples:
  semgrep_api.py findings --repo acme/app --untriaged --json
  semgrep_api.py --json findings --repo acme/app --type sca --status all
  semgrep_api.py projects --repo acme/app
  semgrep_api.py summary --repo acme/app --repo acme/api
  semgrep_api.py lockfiles --repo acme/app
  semgrep_api.py baseline --log /tmp/semgrep-app.log --json
  semgrep_api.py baseline --run --dir ~/work/app --log /tmp/semgrep-app.log \\
      --repo acme/app --json
  semgrep_api.py triage --type sast --ids 1234,1240 --state ignored \\
      --reason false_positive --note "Input is a constant list."

API reference: https://semgrep.dev/api/v1/public_v1.openapi.yaml
Standard library only. `projects` needs an authenticated `gh` CLI unless
--head is given.
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import _gh

BASE = "https://semgrep.dev/api/v1"
TYPES = ("sast", "sca", "secrets")
# The statuses of a finding that is not closed.
ACTIVE = ("open", "fixing", "reviewing")
STATUSES = ("open", "fixing", "reviewing", "ignored", "fixed", "provisionally_ignored", "all")
PAGE = 500


class ApiError(RuntimeError):
    pass


def token():
    t = os.environ.get("SEMGREP_APP_TOKEN", "").strip()
    if not t:
        raise ApiError("Set SEMGREP_APP_TOKEN to a Semgrep token with Web API access.")
    return t


def build_url(path, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    # BASE is a constant. Keep the https guarantee if someone edits it.
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing a non-https URL: {url}")
    return url


def call(method, path, params=None, body=None):
    req = urllib.request.Request(build_url(path, params), method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "sadr",
    }, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as f:
            data = f.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        e.close()
        raise ApiError(f"{method} {path}: HTTP {e.code}: {detail}") from None


def deployments():
    return call("GET", "/deployments").get("deployments", [])


def pick_deployment(arg=None, listing=None):
    """Return the deployment record to use: the named one, or the only one."""
    want = arg or os.environ.get("SEMGREP_DEPLOYMENT", "").strip() or None
    deps = listing if listing is not None else deployments()
    if want:
        for d in deps:
            if want in (d.get("slug"), str(d.get("id"))):
                return d
        raise ApiError(f"the token does not reach deployment {want}")
    if len(deps) == 1:
        return deps[0]
    if not deps:
        raise ApiError("the token reaches no deployment. It needs Web API access.")
    raise ApiError("the token reaches more than one deployment. Pass --deployment or set SEMGREP_DEPLOYMENT.")


def projects(dep, repos=()):
    """The deployment's projects, or only those named in REPOS, in REPOS order."""
    out, page = [], 0
    while True:
        batch = call("GET", f"/deployments/{dep['slug']}/projects", {"page": page, "page_size": PAGE}).get("projects", [])
        out += batch
        if len(batch) < PAGE:
            break
        page += 1
    if not repos:
        return sorted(out, key=lambda p: p.get("name", "").lower())
    by_name = {p.get("name", "").lower(): p for p in out}
    return [by_name.get(r.lower()) or {"name": r, "missing": True} for r in repos]


def full_ref(ref):
    """'main' -> 'refs/heads/main'. A ref that starts with refs/ stays."""
    return ref if not ref or ref.startswith("refs/") else "refs/heads/" + ref


def ref_groups(dep, repos, ref=None, all_refs=False):
    """Return [(ref or None, [repo, ...])] for the findings calls.

    The default ref of a repo is its project's primary branch, so findings
    on refs/pull/N/merge stay out. A repo with no project gets ref None,
    and the caller drops pull-request refs from its findings.
    """
    if all_refs:
        return [(None, list(repos))]
    if ref:
        return [(full_ref(ref), list(repos))]
    groups = {}
    for p in projects(dep, repos):
        branch = None if p.get("missing") else p.get("primary_branch") or p.get("default_branch")
        groups.setdefault(branch, []).append(p["name"])
    return sorted(groups.items(), key=lambda g: g[0] or "")


def secrets(dep, repos, status):
    """Secrets findings. They have their own endpoint, keyed by numeric id, with cursor pages.

    The secrets endpoint knows open, ignored, fixed and provisionally_ignored.
    It has no fixing or reviewing status, so those return nothing.
    """
    if status in ("fixing", "reviewing"):
        return []
    out, cursor = [], None
    while True:
        params = {"limit": PAGE}
        if status and status != "all":
            params["status"] = "FINDING_STATUS_" + status.upper()
        if repos:
            params["repo"] = repos
        if cursor:
            params["cursor"] = cursor
        resp = call("GET", f"/deployments/{dep['id']}/secrets", params)
        out += resp.get("findings", [])
        cursor = resp.get("cursor")
        if not cursor:
            return out


def findings_one(dep, repos, issue_type, status, ref=None):
    """One findings query: one type, one status ("all" for every status), one ref."""
    if issue_type == "secrets":
        return secrets(dep, repos, status)
    out, page = [], 0
    while True:
        params = {"issue_type": issue_type, "page": page, "page_size": PAGE, "dedup": "true"}
        if status and status != "all":
            params["status"] = status
        if repos:
            params["repos"] = repos
        if ref:
            params["ref"] = ref
        batch = call("GET", f"/deployments/{dep['slug']}/findings", params).get("findings", [])
        out += batch
        if len(batch) < PAGE:
            return out
        page += 1


def findings(dep, repos, issue_types=TYPES, statuses=ACTIVE, ref=None, all_refs=False):
    """Findings of every type in ISSUE_TYPES and status in STATUSES, each tagged with its type.

    By default only findings on each repo's primary branch count. A
    finding on refs/pull/N/merge belongs to that PR, not to the repo.
    """
    if isinstance(issue_types, str):
        issue_types = TYPES if issue_types == "all" else (issue_types,)
    if isinstance(statuses, str):
        statuses = (statuses,)
    if "all" in statuses:
        statuses = ("all",)
    out, seen = [], set()
    for want_ref, group in ref_groups(dep, repos, ref, all_refs):
        for t in issue_types:
            for st in statuses:
                for f in findings_one(dep, group, t, st, want_ref):
                    f_ref = f.get("ref") or ""
                    if want_ref and f_ref and full_ref(f_ref) != want_ref:
                        continue
                    if not want_ref and not all_refs and f_ref.startswith("refs/pull/"):
                        continue
                    key = (t, f.get("id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"type": t, **f})
    return out


def triage_state(f):
    return (f.get("triage_state") or f.get("triageState") or "").lower()


def is_untriaged(f):
    return triage_state(f) in ("", "untriaged", "triage_state_untriaged", "reopened")


def repo_of(f):
    repo = f.get("repository") or {}
    return repo.get("name") or f.get("repositoryName") or "?"


def line_for(f):
    """One tab-separated line that describes a finding."""
    loc = f.get("location") or {}
    rule = f.get("rule_name") or (f.get("rule") or {}).get("name") or f.get("type_name") or f.get("ruleHashId")
    sca = f.get("found_dependency") or {}
    where = f"{loc.get('file_path')}:{loc.get('line')}" if loc.get("file_path") else (
        sca.get("lockfile_line_url") or f.get("findingPath") or "")
    fixes = ", ".join(f"{x.get('package')} {x.get('version')}" for x in f.get("fix_recommendations") or []
                      if isinstance(x, dict)) or ""
    pkg = f" {sca.get('package')}@{sca.get('version')} -> {fixes or 'no fix'}" if sca else ""
    vuln = f" {f['vulnerability_identifier']}" if f.get("vulnerability_identifier") else ""
    return (f"{f.get('id')}\t{f.get('type', '?')}\t{repo_of(f)}\t{f.get('severity')}\t"
            f"{f.get('status')}/{triage_state(f) or 'untriaged'}\t{rule}{vuln}\t{where}{pkg}")


def summarize(rows_by_type, repos=()):
    """{repo: {type: {state: count}}} and the total of untriaged findings.

    Every repo in REPOS gets a row, with empty counts when it has no findings.
    """
    counts, total = {r: {t: {} for t in TYPES} for r in repos}, 0
    for t, rows in rows_by_type.items():
        for f in rows:
            state = "untriaged" if is_untriaged(f) else (triage_state(f) or f.get("status") or "?")
            bucket = counts.setdefault(repo_of(f), {x: {} for x in TYPES}).setdefault(t, {})
            bucket[state] = bucket.get(state, 0) + 1
            total += state == "untriaged"
    return counts, total


def scans_of(dep, project):
    """The recent scans of a project's primary branch, any status."""
    branch = project.get("primary_branch") or project.get("default_branch")
    body = {"deployment_id": str(dep["id"]), "repository_id": int(project["id"]), "limit": 20}
    if branch:
        body["branch"] = branch
    return call("POST", f"/deployments/{dep['id']}/scans/search", body=body).get("scans", [])


def latest_scan(dep, project, scans=None):
    """The newest completed scan of a project's primary branch in the last 30 days, or None."""
    scans = scans_of(dep, project) if scans is None else scans
    done = [s for s in scans if s.get("status") == "SCAN_STATUS_COMPLETED" and not s.get("is_partial_scan")]
    done.sort(key=lambda s: s.get("completed_at") or s.get("started_at") or "", reverse=True)
    return done[0] if done else None


def default_head(repo, branch):
    """The commit at the head of BRANCH on GitHub, through `gh`. Return (sha, None) or (None, reason)."""
    try:
        ref = _gh.api(f"repos/{repo}/git/ref/heads/{branch[len('refs/heads/'):] if branch.startswith('refs/heads/') else branch}")
    except _gh.GhError as e:
        return None, f"cannot read the head of {branch}: {e}"
    sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
    return (sha, None) if sha else (None, f"GitHub has no {branch} in {repo}")


def head_age_hours(repo, head, at=None):
    """Hours since the head commit was committed, or None when GitHub does not say."""
    try:
        c = _gh.api(f"repos/{repo}/commits/{head}") or {}
    except _gh.GhError:
        return None
    when = (((c.get("commit") or {}).get("committer")) or {}).get("date")
    return round(_gh.age_days(when, at) * 24, 1) if when else None


def trigger_decision(row, scans, head, age_hours, grace_hours):
    """(touch_trigger, reason). True only when the trigger is the likely cause of a stale scan."""
    if row.get("stale") is not True:
        return False, "the scan is not stale" if row.get("stale") is False else row.get("reason")
    of_head = [s for s in scans if s.get("commit") == head]
    if any(s.get("status") in ("SCAN_STATUS_RUNNING", "SCAN_STATUS_QUEUED", "SCAN_STATUS_PENDING",
                               "SCAN_STATUS_STARTED") for s in of_head):
        return False, "a scan of the head is running"
    if any(s.get("status") not in ("SCAN_STATUS_COMPLETED",) for s in of_head):
        return False, "the scan of the head failed: fix the scan, not the trigger"
    if any(s.get("is_partial_scan") for s in of_head):
        return False, "the head got only a partial (diff-aware) scan: the trigger ran"
    if age_hours is None:
        return None, "cannot read the head commit's time"
    if age_hours < grace_hours:
        return False, f"the head is {age_hours:g} hours old, within the {grace_hours:g}-hour grace"
    return True, f"no scan of the head {head[:7]}, {age_hours:g} hours after its commit"


def project_row(dep, project, head=None, grace_hours=6, at=None):
    """A project with its latest primary-branch scan and whether that scan is stale.

    A scan is stale when its commit is not the head of the default branch.
    touch_trigger says whether the scan trigger is the likely cause.
    """
    row = {"name": project["name"], "primary_branch": project.get("primary_branch"),
           "latest_scan_at": project.get("latest_scan_at"), "tags": project.get("tags") or []}
    if project.get("missing"):
        return {**row, "stale": True, "reason": "Semgrep has no project for this repo", "touch_trigger": False,
                "trigger_reason": "Semgrep has no project for this repo"}
    scans = scans_of(dep, project)
    scan = latest_scan(dep, project, scans)
    branch = row["primary_branch"] or "refs/heads/main"
    if head is None:
        head, why = default_head(project["name"], branch)
    else:
        why = None
    row.update({"scan_commit": scan.get("commit") if scan else None,
                "scan_at": (scan.get("completed_at") or scan.get("started_at")) if scan else None,
                "scan_products": scan.get("enabled_products", []) if scan else [],
                "scan_full": scan.get("is_full_scan") if scan else None,
                "head": head})
    if not scan:
        row.update(stale=True, reason=f"no completed scan of {branch} in the last 30 days")
    elif not head:
        row.update(stale=None, reason=why)
    else:
        row["stale"] = scan.get("commit") != head
        if row["stale"]:
            row["reason"] = f"the latest scan of {branch} is {scan.get('commit', '')[:7]}, the head is {head[:7]}"
    age = head_age_hours(project["name"], head, at) if head and row.get("stale") else None
    row["touch_trigger"], row["trigger_reason"] = trigger_decision(row, scans, head, age, grace_hours)
    return row


def lockfiles(dep, project):
    """The lockfiles that Semgrep parsed in a project, with their dependency counts."""
    out, cursor = [], None
    while True:
        body = {"deploymentId": str(dep["id"]), "repositoryId": str(project["id"])}
        if cursor:
            body["cursor"] = cursor
        resp = call("POST", f"/deployments/{dep['id']}/dependencies/repositories/{project['id']}/lockfiles", body=body)
        out += [{"repo": project["name"], "lockfile": x.get("lockfilePath"), "dependencies": x.get("numDependencies")}
                for x in resp.get("lockfileSummaries", [])]
        cursor = resp.get("cursor")
        if not resp.get("hasMore") or not cursor:
            return out


FINDINGS_BLOB = "Would have sent findings and ignores blob:"
COMPLETE_BLOB = "Would have sent complete blob:"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def blobs(text, marker):
    """Every JSON object that follows MARKER in a log text."""
    text = ANSI.sub("", text)
    out, dec, i = [], json.JSONDecoder(), 0
    while True:
        i = text.find(marker, i)
        if i < 0:
            return out
        j = i + len(marker)
        try:
            obj, _ = dec.raw_decode(text[j:].lstrip())
            out.append(obj)
        except ValueError:
            out.append(None)
        i = j


def finding_type(f):
    meta = f.get("metadata") or {}
    product = (meta.get("product") or "").lower()
    if f.get("sca_info") or product in ("supply-chain", "sca") or (f.get("check_id") or "").startswith("ssc-"):
        return "sca"
    if product == "secrets" or "secrets" in str((meta.get("semgrep.dev") or {}).get("rule", {}).get("origin", "")) \
            or (f.get("validation_state") not in (None, "NO_VALIDATOR")):
        return "secrets"
    return "sast"


def as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def normalize(f, ignored=False):
    """One baseline row from a blob finding."""
    meta = f.get("metadata") or {}
    row = {"type": finding_type(f), "rule": f.get("check_id"), "path": f.get("path"), "line": f.get("line"),
           "end_line": f.get("end_line"), "severity": f.get("severity"), "blocking": bool(f.get("is_blocking")),
           "ignored": ignored, "message": (f.get("message") or "")[:300],
           "syntactic_id": f.get("syntactic_id"), "match_based_id": f.get("match_based_id")}
    if row["type"] == "sca":
        sca = f.get("sca_info") or {}
        dep = (sca.get("dependency_match") or {}).get("found_dependency") or {}
        row.update({"package": dep.get("package"), "version": dep.get("version"),
                    "ecosystem": dep.get("ecosystem"),
                    "lockfile": (sca.get("dependency_match") or {}).get("lockfile") or dep.get("lockfile_path"),
                    "reachable": sca.get("reachable"),
                    "transitivity": dep.get("transitivity"), "sca_severity": meta.get("sca-severity"),
                    "advisories": list(dict.fromkeys(x for k in ("sca-vuln-database-identifier", "ghsa", "cve")
                                                     for x in as_list(meta.get(k)) if x)),
                    "fixed_versions": meta.get("sca-fix-versions")})
    if row["type"] == "secrets":
        row["validation_state"] = f.get("validation_state")
    return row


def from_json_output(data):
    """Baseline rows from a --json-output file (semgrep's CLI JSON). It lacks non-blocking findings."""
    out = []
    for r in (data or {}).get("results") or []:
        extra = r.get("extra") or {}
        f = {"check_id": r.get("check_id"), "path": r.get("path"), "line": (r.get("start") or {}).get("line"),
             "end_line": (r.get("end") or {}).get("line"), "message": extra.get("message"),
             "metadata": extra.get("metadata"), "sca_info": extra.get("sca_info"),
             "validation_state": extra.get("validation_state"),
             "is_blocking": "block" in str(((extra.get("metadata") or {}).get("dev.semgrep.actions")) or ""),
             "severity": extra.get("severity")}
        out.append(normalize(f, ignored=bool(extra.get("is_ignored"))))
    return out


def row_key(r):
    if r["type"] == "sca":
        return ("sca", r.get("package"), r.get("version"), tuple(sorted(r.get("advisories") or [])) or r["rule"])
    return (r["type"], r["rule"], r["path"], r["line"])


def parse_baseline(log_text, json_output=None):
    """Read a `semgrep ci --dry-run` log. Return {ok, problems, findings, counts, ...}.

    ok is false when the log has no findings blob, a blob does not parse,
    or the scan reports a fatal error.
    """
    problems = []
    fblobs = blobs(log_text, FINDINGS_BLOB)
    cblobs = blobs(log_text, COMPLETE_BLOB)
    if not fblobs:
        problems.append("the log has no findings blob: the scan did not finish, or did not run with --dry-run")
    if any(b is None for b in fblobs + cblobs):
        problems.append("a blob in the log does not parse as JSON")
    rows, seen = [], set()

    def add(r):
        k = row_key(r)
        if k in seen:
            return
        seen.add(k)
        rows.append(r)

    for b in (x for x in fblobs if x):
        for f in b.get("findings") or []:
            add(normalize(f))
        for f in b.get("ignores") or []:
            add(normalize(f, ignored=True))
    from_file = 0
    if json_output is not None:
        before = len(rows)
        for r in from_json_output(json_output):
            add(r)
        from_file = len(rows) - before
    complete = next((b for b in reversed(cblobs) if b), {})
    exit_code = complete.get("exit_code")
    errors = ((complete.get("stats") or {}).get("errors")) or []
    fatal = [e for e in errors if str((e or {}).get("level", "")).lower() in ("error", "fatal")
             or "fatal" in str((e or {}).get("type", "")).lower()]
    if fatal:
        problems.append(f"the scan reports {len(fatal)} fatal errors, for example: "
                        f"{str(fatal[0].get('message') or fatal[0].get('type'))[:200]}")
    if exit_code not in (None, 0, 1):
        problems.append(f"the scan would have exited {exit_code}")
    if cblobs and not complete:
        problems.append("the complete blob does not parse")
    counts = {t: {"total": 0, "blocking": 0, "ignored": 0} for t in TYPES}
    for r in rows:
        c = counts[r["type"]]
        c["total"] += 1
        c["blocking"] += r["blocking"]
        c["ignored"] += r["ignored"]
    return {"ok": not problems, "problems": problems, "exit_code": exit_code,
            "findings_by_product": (complete.get("stats") or {}).get("findings_by_product"),
            "counts": counts, "from_json_output_only": from_file, "findings": rows}


def match_api(rows, api_rows):
    """Add api_id, api_status and api_triage_state to each baseline row that the platform also has."""
    index = {}
    for f in api_rows:
        t = f.get("type") or ("sca" if f.get("found_dependency") else "sast")
        if t == "sca":
            dep = f.get("found_dependency") or {}
            vuln = f.get("vulnerability_identifier")
            keys = [("sca", dep.get("package"), dep.get("version"), vuln)]
        else:
            loc = f.get("location") or {}
            rule = f.get("rule_name") or (f.get("rule") or {}).get("name") or f.get("type_name") or ""
            keys = [(t, rule, loc.get("file_path") or f.get("findingPath")),
                    (t, rule.rsplit(".", 1)[-1], loc.get("file_path") or f.get("findingPath"))]
        for k in keys:
            index.setdefault(k, []).append(f)
    for r in rows:
        if r["type"] == "sca":
            cands = []
            for adv in r.get("advisories") or [None]:
                cands += index.get(("sca", r.get("package"), r.get("version"), adv), [])
        else:
            cands = index.get((r["type"], r["rule"], r["path"]), []) or index.get(
                (r["type"], (r["rule"] or "").rsplit(".", 1)[-1], r["path"]), [])
        if cands:
            best = min(cands, key=lambda f: abs(((f.get("location") or {}).get("line") or 0) - (r.get("line") or 0)))
            r.update(api_id=best.get("id"), api_status=best.get("status"),
                     api_triage_state=triage_state(best) or "untriaged")
        else:
            r.update(api_id=None, api_status=None, api_triage_state=None)
    return rows


def run_dry_run(folder, log_path, timeout=1800):
    """Run `semgrep ci --dry-run` in FOLDER, with its output in LOG_PATH. Return the json-output path.

    Raise ApiError when semgrep is not on PATH or the scan times out.
    """
    exe = shutil.which("semgrep")
    if not exe:
        raise ApiError("semgrep is not on PATH. Install a pinned semgrep (see references/scanning.md); "
                       "this script never installs it.")
    token()
    json_path = log_path + ".json"
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            subprocess.run([exe, "ci", "--dry-run", f"--json-output={json_path}"], cwd=folder, stdout=log,
                           stderr=subprocess.STDOUT, timeout=timeout, env={**os.environ, "NO_COLOR": "1"})
        except subprocess.TimeoutExpired:
            raise ApiError(f"semgrep ci --dry-run ran longer than {timeout} seconds; see {log_path}") from None
    return json_path


def triage_body(issue_type, ids, state=None, reason=None, note=None):
    """Build the triage request body. Raise ValueError on a bad combination."""
    if not (state or note):
        raise ValueError("pass --state, --note, or both")
    if state == "ignored" and not reason:
        raise ValueError("--reason is required with --state ignored")
    if reason and state != "ignored":
        raise ValueError("--reason goes only with --state ignored")
    try:
        id_list = [int(i) for i in ids.split(",") if i.strip()]
    except ValueError:
        raise ValueError("--ids takes numbers separated by commas") from None
    if not id_list:
        raise ValueError("--ids is empty")
    body = {"issue_type": issue_type, "issue_ids": id_list}
    if state:
        body["new_triage_state"] = state
    if note:
        body["new_note"] = note[:3000]
    if reason:
        body["new_triage_reason"] = reason
    return body


def emit(data, as_json, text_lines):
    if as_json:
        json.dump(data, sys.stdout, indent=2)
        print()
    else:
        for line in text_lines:
            print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    _gh.add_json(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--deployment", help="deployment slug or id (default: $SEMGREP_DEPLOYMENT, or the only one)")
        _gh.add_json(p, sub=True)
        return p

    def add_filters(p):
        p.add_argument("--repo", action="append", default=[], help="OWNER/NAME (repeat for more)")
        p.add_argument("--status", action="append", choices=STATUSES,
                       help="a status to read (repeat for more). Default: open, fixing and reviewing")
        p.add_argument("--ref", help="read findings on this branch or ref (default: each project's primary branch)")
        p.add_argument("--all-refs", action="store_true", help="read findings on every ref, pull requests included")

    add("deployments", "list the deployments that the token reaches")
    p = add("projects", "list the projects and whether their latest scan is stale")
    p.add_argument("--repo", action="append", default=[], help="OWNER/NAME (repeat for more)")
    p.add_argument("--head", metavar="SHA", help="the default-branch head to compare with (only with one --repo)")
    p.add_argument("--grace-hours", type=float, default=6,
                   help="touch_trigger stays false while the head commit is younger than this (default: 6)")
    p = add("findings", "list findings")
    add_filters(p)
    p.add_argument("--type", default="all", choices=TYPES + ("all",), help="the finding type (default: all)")
    p.add_argument("--untriaged", action="store_true", help="only findings with no triage decision")
    p = add("summary", "count findings per repo by triage state")
    add_filters(p)
    p = add("lockfiles", "list the lockfiles that Semgrep parsed, with dependency counts")
    p.add_argument("--repo", action="append", required=True, help="OWNER/NAME (repeat for more)")
    p = add("baseline", "read the findings of a local semgrep ci --dry-run from its log")
    p.add_argument("--log", required=True, help="the log of semgrep ci --dry-run (written there with --run)")
    p.add_argument("--json-file", help="the file that --json-output wrote, to join in")
    p.add_argument("--run", action="store_true", help="run semgrep ci --dry-run in --dir first")
    p.add_argument("--dir", default=".", help="with --run, the clone to scan (default: the current folder)")
    p.add_argument("--timeout", type=int, default=1800, help="with --run, the time limit in seconds")
    p.add_argument("--api-json", help="the output of findings --status all --json, to match against")
    p.add_argument("--repo", help="OWNER/NAME: read the platform findings (all statuses) to match against")
    p = add("triage", "record a triage decision")
    p.add_argument("--type", required=True, choices=TYPES)
    p.add_argument("--ids", required=True, help="finding ids separated by commas")
    # Semgrep keeps "reviewing" and "fixing" in private beta. If it refuses them, send --note alone.
    p.add_argument("--state", choices=["ignored", "reviewing", "fixing", "reopened"])
    p.add_argument("--reason", choices=["false_positive", "acceptable_risk", "duplicate"])
    p.add_argument("--note", help="the triage note, up to 3000 characters")
    p.add_argument("--dry-run", action="store_true", help="print the request and send nothing")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "triage":
            try:
                body = triage_body(a.type, a.ids, a.state, a.reason, a.note)
            except ValueError as e:
                ap.error(str(e))
            if a.dry_run:
                print(json.dumps({"deployment": a.deployment or os.environ.get("SEMGREP_DEPLOYMENT")
                                  or "<the token's deployment>", **body}, indent=2))
                return
            dep = pick_deployment(a.deployment)
            emit(call("POST", f"/deployments/{dep['slug']}/triage", body=body), True, [])
        elif a.cmd == "baseline":
            json_file = a.json_file
            if a.run:
                json_file = run_dry_run(a.dir, a.log, a.timeout)
            with open(a.log, encoding="utf-8", errors="replace") as f:
                text = f.read()
            jdata = None
            if json_file and os.path.exists(json_file) and os.path.getsize(json_file):
                with open(json_file, encoding="utf-8") as f:
                    jdata = json.load(f)
            res = parse_baseline(text, jdata)
            api_rows = None
            if a.api_json:
                with open(a.api_json, encoding="utf-8") as f:
                    api_rows = json.load(f)
            elif a.repo:
                dep = pick_deployment(a.deployment)
                api_rows = findings(dep, [a.repo], "all", ("all",))
            if api_rows is not None:
                match_api(res["findings"], api_rows)
                res["matched_api"] = True
            lines = [f"{r['type']}\t{r['rule']}\t{r['path']}:{r['line']}"
                     + (f"\t{r.get('package')}@{r.get('version')} {','.join(r.get('advisories') or [])}"
                        if r["type"] == "sca" else "")
                     + ("\tblocking" if r["blocking"] else "") + ("\tignored" if r["ignored"] else "")
                     + (f"\tapi={r.get('api_id')} {r.get('api_triage_state')}" if res.get("matched_api") else "")
                     for r in res["findings"]]
            emit(res, a.json, lines)
            if not a.json:
                print("; ".join(f"{t}: {c['total']} ({c['blocking']} blocking, {c['ignored']} ignored)"
                                for t, c in res["counts"].items()), file=sys.stderr)
                for p in res["problems"]:
                    print(f"not a baseline: {p}", file=sys.stderr)
            if not res["ok"]:
                sys.exit(2)
        elif a.cmd == "deployments":
            deps = deployments()
            emit(deps, a.json, [f"{d.get('slug')}\t{d.get('name')}\tid={d.get('id')}" for d in deps])
        elif a.cmd == "projects":
            if a.head and len(a.repo) != 1:
                ap.error("--head goes with exactly one --repo")
            dep = pick_deployment(a.deployment)
            projs = projects(dep, a.repo)
            with concurrent.futures.ThreadPoolExecutor(8) as ex:
                rows = list(ex.map(lambda p: project_row(dep, p, a.head, a.grace_hours), projs))

            def text(r):
                stale = {True: "yes", False: "no", None: "unknown"}[r.get("stale")]
                return (f"{r['name']}\tbranch={(r.get('primary_branch') or '-').replace('refs/heads/', '')}\t"
                        f"scan={(r.get('scan_commit') or '-')[:12]} at {r.get('scan_at') or '-'}\t"
                        f"head={(r.get('head') or '-')[:12]}\tstale={stale}\t"
                        f"products={','.join(r.get('scan_products') or []) or '-'}"
                        + (f"\ttags={','.join(r['tags'])}" if r.get("tags") else "")
                        + (f"\t{r['reason']}" if r.get("reason") else "")
                        + f"\ttouch_trigger={ {True: 'yes', False: 'no', None: 'unknown'}[r.get('touch_trigger')] }")
            emit(rows, a.json, [text(r) for r in rows])
        elif a.cmd == "findings":
            dep = pick_deployment(a.deployment)
            statuses = a.status or ACTIVE
            rows = [f for f in findings(dep, a.repo, a.type, statuses, a.ref, a.all_refs)
                    if not a.untriaged or is_untriaged(f)]
            emit(rows, a.json, [line_for(f) for f in rows])
            if not a.json:
                print(f"{len(rows)} findings (types: {a.type}, statuses: {', '.join(statuses)}, "
                      f"ref: {'all' if a.all_refs else a.ref or 'primary branch'})", file=sys.stderr)
        elif a.cmd == "summary":
            dep = pick_deployment(a.deployment)
            statuses = a.status or ACTIVE
            projs = projects(dep, a.repo)
            names = [p["name"] for p in projs]
            rows = findings(dep, a.repo, "all", statuses, a.ref, a.all_refs)
            counts, total = summarize({t: [f for f in rows if f["type"] == t] for t in TYPES}, names)
            scans = {p["name"]: p.get("latest_scan_at") for p in projs}
            lines = []
            for repo in sorted(counts, key=str.lower):
                parts = [f"{t}: " + (", ".join(f"{k} {v}" for k, v in sorted(s.items())) or "none")
                         for t, s in counts[repo].items()]
                lines.append(f"{repo}\t" + "; ".join(parts) + f"\tlatest_scan={scans.get(repo) or 'none'}")
            emit({"deployment": dep.get("slug"), "statuses": list(statuses), "untriaged": total, "repos": counts,
                  "latest_scan_at": scans}, a.json, lines)
            if not a.json:
                print(f"{len(counts)} repos, {total} untriaged findings ({', '.join(statuses)})", file=sys.stderr)
        elif a.cmd == "lockfiles":
            dep = pick_deployment(a.deployment)
            rows, missing = [], []
            for p in projects(dep, a.repo):
                if p.get("missing"):
                    missing.append(p["name"])
                    continue
                rows += lockfiles(dep, p)
            emit({"lockfiles": rows, "no_project": missing} if a.json else rows, a.json,
                 [f"{r['repo']}\t{r['lockfile']}\t{r['dependencies']} dependencies" for r in rows]
                 + [f"{m}\tSemgrep has no project for this repo" for m in missing])
    except ApiError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
