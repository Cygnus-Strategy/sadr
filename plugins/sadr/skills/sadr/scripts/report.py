#!/usr/bin/env python3
"""Build Sadr's run history and report from its PRs on GitHub.

Sadr keeps no state of its own. A Sadr PR comes from a branch that starts
with sadr/ and has a title that starts with "sadr:". Its body uses the
fixed sections in references/pr-format.md. A run opens up to three kinds
of PR, each on its own branch off the default branch:

  facts    sadr/facts, "sadr: AGENTS.md facts YYYY-MM-DD"
  upkeep   sadr/upkeep, "sadr: upkeep YYYY-MM-DD"
  issue    sadr/issue-<N> or sadr/issue-<N>-<part>, "sadr: #N ..."

This script finds those PRs, reads the sections, and builds these datasets:

  runs      one row per run date, with PR counts per kind, finding, time
            and spend totals
  prs       one row per PR, with its kind, state, CI result and wait time
  items     one row per "Needs you" bullet
  holds     one row per "Holds" bullet
  findings  one row per PR, with the counts from "Security findings"
  account   account-wide gaps, such as "turn on Semgrep Secrets", once each

The date of a facts or upkeep PR's run comes from its title. An issue PR
belongs to the run on the date it merged, or else the date it was last
updated.

An account-wide gap is listed once in the report, never per PR. It comes
from --gaps FILE, from PREVIEW_DIR/account-gaps.md in a preview, or from
a "Needs you" bullet that starts with "Account-wide:". Such bullets leave
the per-PR items.

The script searches twice, by branch prefix and by title prefix, and joins
the results. GitHub search sometimes returns fewer items than its own total
count. The script retries the search, and exits 2 if it is still short.

A preview run opens no PRs. It writes each PR body to a local file
instead: PREVIEW_DIR/OWNER__NAME/<branch name after sadr/>.md, that is
facts.md, upkeep.md or issue-<N>[-<part>].md, with the PR title on the
first line. comment-issue-<N>.md is a comment that Sadr would post, and
no-pr.md is a note on why the repo got no PR. --bodies DIR reads these
files as PRs in state PREVIEW. A PR file without a "sadr:" title line is
still read as a PR, and the report lists it under "Preview problems".
--bodies reads only the repos in scope: the --repo, --org and --user
arguments and the repos in --repos. Pass --all-bodies to read every repo.

--new-preview-dir prints a new, empty, unique PREVIEW_DIR and exits. Its
name holds the UTC start time and a run id, so concurrent runs never share
one.

--legacy-prefix PREFIX also reads PRs from branches that start with PREFIX,
for example factory/ for the runs before Sadr. Their run date comes from a
date in the branch name.

Minutes and spend stay empty (null in JSON) when no PR of a run reports
them. The report never shows an unknown spend as 0.

Examples:
  report.py --org acme
  report.py --org acme --user octocat --format json --out report/
  report.py --org acme --repos repos.json   (adds opted-out repos)
  PREVIEW_DIR=$(report.py --new-preview-dir)
  report.py --repos repos.json --bodies "$PREVIEW_DIR" --format md
  report.py --org acme --gaps gaps.md
  report.py --org acme --legacy-prefix factory/

Needs an authenticated `gh` CLI. Standard library only.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import tempfile
import time

import _gh

SEARCH_LIMIT = 1000
SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title body url state isDraft createdAt updatedAt mergedAt closedAt
        headRefName reviewDecision
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}"""
SECTION_ALIASES = {"semgrep": "security findings"}
UPKEEP_TITLE = re.compile(r"sadr:\s*upkeep\s+(\d{4}-\d{2}-\d{2})", re.I)
FACTS_TITLE = re.compile(r"sadr:\s*AGENTS\.md\s+facts\s+(\d{4}-\d{2}-\d{2})", re.I)
FACTS_BRANCH = "sadr/facts"
UPKEEP_BRANCH = "sadr/upkeep"
ISSUE_BRANCH = re.compile(r"sadr/issue-(\d+)(?:-[\w.-]+)?$")
DATED_BRANCH = re.compile(r"sadr/(\d{4}-\d{2}-\d{2})")
ISSUE_TITLE = re.compile(r"sadr:\s*#(\d+)")
# Preview file names. feature-<N> is the name before issue-<N>.
ISSUE_FILE = re.compile(r"(?:issue|feature)-(\d+)(?:-([\w.-]+))?\.md$")
COMMENT_FILE = re.compile(r"comment-issue-(\d+)(?:-[\w.-]+)?\.md$")
NOTE_FILES = ("no-pr.md",)
GAPS_FILE = "account-gaps.md"
# A PR body has at least one of these pr-format headings.
PR_HEADING = re.compile(r"^## (What this PR does|Needs you|Changes|Security findings|Run)\s*$", re.M | re.I)
ACCOUNT_PREFIX = re.compile(r"account[- ]wide:\s*", re.I)
KINDS = ("facts", "upkeep", "issue")
BRANCH_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
OPEN_STATES = ("OPEN", "PREVIEW")
NONE_LINE = re.compile(r"none\.?", re.I)


class IncompleteSearch(RuntimeError):
    pass


def search_queries(qualifier, legacy=()):
    """Two searches: one by branch prefix, one by title prefix. One more for each legacy prefix."""
    return [f"{qualifier} is:pr head:sadr/ sort:created-asc",
            f'{qualifier} is:pr in:title "sadr:" sort:created-asc'] + [
        f"{qualifier} is:pr head:{p} sort:created-asc" for p in legacy]


def search_one(q, attempts, sleep):
    """Return every PR node for one search query.

    Retry when the search returns fewer nodes than its issueCount. Raise
    IncompleteSearch when it is still short after ATTEMPTS tries.
    """
    got, total, err = [], 0, ""
    for attempt in range(attempts):
        got, after, total = [], None, 0
        try:
            while True:
                data = _gh.graphql(SEARCH, q=q, after=after)["search"]
                total = data["issueCount"]
                got += [n for n in data["nodes"] if n]
                if not data["pageInfo"]["hasNextPage"]:
                    break
                after = data["pageInfo"]["endCursor"]
        except (_gh.GhError, KeyError, TypeError) as e:
            got, err = [], f" ({e})"
            total = max(total, 1)
        if total > SEARCH_LIMIT:
            raise IncompleteSearch(f"{q}: {total} PRs, more than the search limit of {SEARCH_LIMIT}")
        if len(got) >= total:
            return got
        if attempt < attempts - 1:
            sleep(20 * (attempt + 1))
    raise IncompleteSearch(f"{q}: the search returned {len(got)} of {total} PRs{err}")


def search_all(qualifier, attempts=4, sleep=time.sleep, legacy=()):
    """Return the union of both searches for one owner qualifier.

    GitHub search sometimes returns zero results for a query that has
    results. Two different queries make that less likely to go unnoticed.
    When both return nothing, the script searches once more after a pause.
    """
    found = {}
    for round_ in range(2):
        for q in search_queries(qualifier, legacy):
            for n in search_one(q, attempts, sleep):
                found.setdefault(n.get("url"), n)
        if found or round_:
            break
        sleep(10)
    return list(found.values())


def is_sadr_pr(node, legacy=()):
    branch = node.get("headRefName") or ""
    if legacy and branch.startswith(tuple(legacy)):
        return True
    return branch.startswith("sadr/") and (node.get("title") or "").startswith("sadr:")


def in_scope(repo, scope):
    """True when SCOPE is None, or names REPO or its owner (case-insensitive)."""
    if scope is None:
        return True
    return repo.lower() in scope or repo.split("/")[0].lower() in scope


def read_bodies(folder, scope=None):
    """Read preview files from FOLDER/OWNER__NAME/*.md.

    Return (PR nodes in state PREVIEW, comments, notes, problems). Only the
    repos that SCOPE names (a set of lowercase OWNER/NAME and OWNER) are
    read; None reads all.

    A file whose first line starts with "sadr:" is a PR: the first line is
    its title, the rest its body. A file without that title line but with
    pr-format headings (such as "## What this PR does") is a PR too: its
    title is made up from the file name, and PROBLEMS says so.
    comment-issue-<N>.md, and any other file without a title line and
    without pr-format headings, is a comment that Sadr would post.
    no-pr.md is a note, not a comment.
    """
    nodes, comments, notes, problems = [], [], [], []
    for path in sorted(glob.glob(os.path.join(folder, "*__*", "*.md"))):
        owner, _, name = os.path.basename(os.path.dirname(path)).partition("__")
        repo = f"{owner}/{name}"
        if not in_scope(repo, scope):
            continue
        base = os.path.basename(path)
        rel = os.path.relpath(path, folder)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        first, _, body = text.lstrip().partition("\n")
        title = first.strip()
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
        when = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
        if base in NOTE_FILES:
            notes.append({"repo": repo, "file": rel, "text": text.strip()})
            continue
        cm = COMMENT_FILE.fullmatch(base)
        fm = None if cm else ISSUE_FILE.fullmatch(base)
        stem = base[:-3]
        titled = title.startswith("sadr:")
        if cm or not (titled or PR_HEADING.search(text)):
            comments.append({"repo": repo, "file": rel,
                             "issue": int(cm.group(1)) if cm else (int(fm.group(1)) if fm else None),
                             "text": text.strip()})
            continue
        if not titled:
            problems.append({"repo": repo, "file": rel,
                             "problem": "no PR title on line 1 (it must start with \"sadr:\")"})
            body = text
            day = when[:10]
            title = {"facts": f"sadr: AGENTS.md facts {day}", "upkeep": f"sadr: upkeep {day}"}.get(
                stem, f"sadr: #{fm.group(1)}" if fm else f"sadr: {stem}")
        tm = ISSUE_TITLE.match(title)
        if fm or tm:
            number = fm.group(1) if fm else tm.group(1)
            branch = f"sadr/issue-{number}" + (f"-{fm.group(2)}" if fm and fm.group(2) else "")
        elif stem == "facts" or FACTS_TITLE.match(title):
            branch = FACTS_BRANCH
        elif stem == "upkeep" or UPKEEP_TITLE.match(title):
            branch = UPKEEP_BRANCH
        else:
            branch = "sadr/" + stem
            problems.append({"repo": repo, "file": rel,
                             "problem": "the file name is not facts.md, upkeep.md or issue-<N>.md"})
        nodes.append({"number": None, "title": title, "body": body, "url": rel,
                      "state": "PREVIEW",
                      "isDraft": False, "createdAt": when, "updatedAt": when, "mergedAt": None, "closedAt": None,
                      "headRefName": branch, "reviewDecision": None, "repository": {"nameWithOwner": repo},
                      "commits": {"nodes": []}})
    return nodes, comments, notes, problems


def read_gaps(path):
    """Account-wide gaps from a file: a JSON list of strings, or one "- " bullet per line."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.lstrip().startswith("["):
        return [str(x).strip() for x in json.loads(text) if str(x).strip()]
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s+(.*)", line)
        if m and not NONE_LINE.fullmatch(m.group(1).strip()):
            out.append(ACCOUNT_PREFIX.sub("", m.group(1).strip()))
    return out


def unique_gaps(gaps):
    """GAPS without duplicates (case and spacing ignored), in first-seen order."""
    seen, out = set(), []
    for g in gaps:
        key = re.sub(r"\s+", " ", g.strip().rstrip(".").lower())
        if key and key not in seen:
            seen.add(key)
            out.append(g.strip())
    return out


def new_preview_dir(base=None, run_id=None):
    """Create and return a new, empty, unique preview folder.

    BASE defaults to $RUNNER_TEMP/sadr-preview in GitHub Actions, else
    ${XDG_CACHE_HOME:-~/.cache}/sadr/preview. The name is the UTC start
    time, the run id ($GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT when set), and a
    random suffix.
    """
    if not base:
        if os.environ.get("RUNNER_TEMP"):
            base = os.path.join(os.environ["RUNNER_TEMP"], "sadr-preview")
        else:
            cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
            base = os.path.join(cache, "sadr", "preview")
    os.makedirs(base, exist_ok=True)
    if run_id is None and os.environ.get("GITHUB_RUN_ID"):
        run_id = os.environ["GITHUB_RUN_ID"] + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    stamp = _gh.now().strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{stamp}-" + (f"{re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)}-" if run_id else "")
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def sections(body):
    """{heading: [bullet, ...]} for each '## ' section of a PR body.

    An indented line that is not a bullet continues the bullet before it.
    "- None." bullets are dropped.
    """
    out, cur = {}, None
    for line in (body or "").splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            cur = SECTION_ALIASES.get(cur, cur)
            out[cur] = []
        elif line.startswith("#") or line.strip() == "---":
            cur = None
        elif cur is None:
            continue
        elif re.match(r"[-*] ", line):
            out[cur].append(line[2:].strip())
        elif line.startswith((" ", "\t")) and line.strip() and out[cur]:
            out[cur][-1] += " " + line.strip()
    return {k: [b for b in v if not NONE_LINE.fullmatch(b)] for k, v in out.items()}


def finding_counts(lines):
    """Read the counts from "Security findings" bullets.

    The lines start "untriaged: A → B", "fixed: N", "false positive: N" and
    "accepted risk: N". A missing count stays None (untriaged) or 0.
    """
    c = {"before": None, "after": None, "fixed": 0, "false_positive": 0, "accepted_risk": 0}
    for line in lines:
        low = line.lower()
        m = re.match(r"untriaged:\s*(\d+)\s*(?:→|->)\s*(\d+)", low)
        if m:
            c["before"], c["after"] = int(m.group(1)), int(m.group(2))
            continue
        m = re.match(r"(fixed|false positive|accepted risk|acceptable risk):\s*(.*)", low)
        if m:
            key = {"fixed": "fixed", "false positive": "false_positive"}.get(m.group(1), "accepted_risk")
            c[key] += count_of(m.group(2))
    return c


def count_of(rest):
    """The count at the start of a findings line, after the colon.

    "3 (1234 rule in a.py:8; ...)" gives 3. A line that starts with a finding
    id instead of a count gives the number of ids. Semgrep ids have six or
    more digits.
    """
    m = re.match(r"(\d{1,5})(?:\s*\(|\s*$)", rest)
    if m:
        return int(m.group(1))
    if re.match(r"(none|no\b|0\b)", rest):
        return 0
    return len(re.findall(r"\b\d{6,}\b", rest)) or 1


def run_usage(lines):
    """(minutes, US dollars, billing) from the "Run" section."""
    text = " ".join(lines).lower()
    minutes = None
    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hours?)\b", text)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|minutes?)\b", text)
    if h or m:
        minutes = round(float(h.group(1)) * 60 if h else 0) + round(float(m.group(1)) if m else 0)
    usd = re.search(r"us\$\s*([\d,]+(?:\.\d+)?)", text)
    spend = float(usd.group(1).replace(",", "")) if usd else None
    billing = "subscription" if "subscription" in text else ("api" if spend is not None else None)
    return minutes, spend, billing


def ci_state(node):
    commits = ((node.get("commits") or {}).get("nodes")) or []
    rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    state = (rollup.get("state") or "").upper()
    return {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail",
            "PENDING": "pending", "EXPECTED": "pending"}.get(state, "none")


def pr_row(node, at, legacy=()):
    """Turn one search node into a PR row plus its parsed sections."""
    branch = node["headRefName"]
    kind, issue, run = "other", None, None
    if legacy and branch.startswith(tuple(legacy)):
        kind = "legacy"
        m = BRANCH_DATE.search(branch)
        run = m.group(1) if m else None
    elif branch == FACTS_BRANCH:
        kind = "facts"
        m = FACTS_TITLE.match(node["title"])
        run = m.group(1) if m else None
    elif branch == UPKEEP_BRANCH:
        kind = "upkeep"
        m = UPKEEP_TITLE.match(node["title"])
        run = m.group(1) if m else None
    elif ISSUE_BRANCH.match(branch):
        kind, issue = "issue", int(ISSUE_BRANCH.match(branch).group(1))
    elif DATED_BRANCH.fullmatch(branch):
        kind, run = "upkeep", DATED_BRANCH.fullmatch(branch).group(1)
    if not run:
        # Branches carry no date. Use the merge time, else the last update.
        run = (node.get("mergedAt") or node.get("updatedAt") or node.get("createdAt"))[:10]
    sec = sections(node.get("body"))
    minutes, spend, billing = run_usage(sec.get("run", []))
    state = node["state"]
    row = {
        "run": run, "repo": node["repository"]["nameWithOwner"], "number": node["number"],
        "url": node["url"], "kind": kind, "issue": issue, "title": node["title"], "branch": branch,
        "state": state, "draft": bool(node.get("isDraft")), "ci": ci_state(node),
        "review": (node.get("reviewDecision") or "").lower().replace("_", " "),
        "created_at": node.get("createdAt"), "updated_at": node.get("updatedAt"),
        "merged_at": node.get("mergedAt"),
        "waiting_days": round(_gh.age_days(node["createdAt"], at), 1) if state == "OPEN" else None,
        "minutes": minutes, "spend_usd": spend, "billing": billing,
    }
    return row, sec


def build(nodes, at=None, legacy=(), gaps=()):
    """Build the datasets from search nodes. GAPS are account-wide gaps from elsewhere."""
    at = at or _gh.now()
    prs, items, holds, findings = [], [], [], []
    account = list(gaps)
    with_holds = set()
    seen = set()
    for node in nodes:
        if not is_sadr_pr(node, legacy) or node["url"] in seen:
            continue
        seen.add(node["url"])
        row, sec = pr_row(node, at, legacy)
        prs.append(row)
        base = {"run": row["run"], "repo": row["repo"], "number": row["number"], "url": row["url"]}
        if "holds" in sec:
            with_holds.add(row["url"])
        for text in sec.get("needs you", []):
            if ACCOUNT_PREFIX.match(text):
                account.append(ACCOUNT_PREFIX.sub("", text, count=1))
                continue
            done = text.lower().startswith("done:")
            status = "done" if done or row["state"] not in OPEN_STATES else "open"
            resolution = "marked done" if done else {"MERGED": "PR merged", "CLOSED": "PR closed"}.get(row["state"], "")
            items.append({**base, "item": text, "status": status, "resolution": resolution})
        for text in sec.get("holds", []):
            holds.append({**base, "hold": text})
        if "security findings" in sec:
            findings.append({**base, **finding_counts(sec["security findings"])})
    prs.sort(key=lambda p: (p["run"], p["repo"].lower(), p["number"] or 0, p["url"]))
    # A repo's holds as they stand are the ones in its most recently updated
    # PR that has a Holds section.
    latest = {}
    for p in prs:
        if p["url"] not in with_holds:
            continue
        if p["repo"] not in latest or p["updated_at"] > latest[p["repo"]]["updated_at"]:
            latest[p["repo"]] = p
    for h in holds:
        h["current"] = latest[h["repo"]]["url"] == h["url"]
    return {"runs": runs_from(prs, items, findings), "prs": prs, "items": items,
            "holds": holds, "findings": findings, "account": unique_gaps(account)}


def runs_from(prs, items, findings):
    runs = {}
    for p in prs:
        r = runs.setdefault(p["run"], {
            "run": p["run"], "repos": set(), "prs": 0, "prs_open": 0, "prs_merged": 0, "prs_failing": 0,
            "needs_you_open": 0, "findings_before": None, "findings_after": None,
            "fixed": 0, "false_positive": 0, "accepted_risk": 0, "minutes": None, "spend_usd": None,
            "spend_unknown_prs": 0, "prs_preview": 0, "prs_facts": 0, "prs_upkeep": 0, "prs_issue": 0})
        r["repos"].add(p["repo"])
        r["prs"] += 1
        if p.get("kind") in KINDS:
            r["prs_" + p["kind"]] += 1
        r["prs_open"] += p["state"] in OPEN_STATES
        r["prs_preview"] += p["state"] == "PREVIEW"
        r["prs_merged"] += p["state"] == "MERGED"
        r["prs_failing"] += p["state"] == "OPEN" and p["ci"] == "fail"
        if p["minutes"] is not None:
            r["minutes"] = (r["minutes"] or 0) + p["minutes"]
        if p["spend_usd"] is not None:
            r["spend_usd"] = (r["spend_usd"] or 0) + p["spend_usd"]
        elif p["billing"] != "subscription":
            r["spend_unknown_prs"] += 1
    for i in items:
        runs[i["run"]]["needs_you_open"] += i["status"] == "open"
    for f in findings:
        r = runs[f["run"]]
        for k in ("fixed", "false_positive", "accepted_risk"):
            r[k] += f[k]
        if f["before"] is not None:
            r["findings_before"] = (r["findings_before"] or 0) + f["before"]
            r["findings_after"] = (r["findings_after"] or 0) + f["after"]
    out = []
    for key in sorted(runs, reverse=True):
        r = runs[key]
        r["repos"] = len(r["repos"])
        if r["spend_usd"] is not None:
            r["spend_usd"] = round(r["spend_usd"], 2)
        out.append(r)
    return out


def render_md(data, at, repos=None):
    runs, prs = data["runs"], data["prs"]
    out = [f"# Sadr report {at.date().isoformat()}", ""]
    open_prs = [p for p in prs if p["state"] in OPEN_STATES]
    open_items = [i for i in data["items"] if i["status"] == "open"]
    line = f"{len(prs)} Sadr PRs in {len(runs)} runs. {len(open_prs)} open, {len(open_items)} items need you."
    if repos is not None:
        line += f" {len(repos['repos'])} repos in scope, {sum(r['opted_out'] for r in repos['repos'])} opted out."
    out += [line, ""]

    def num(v):
        return "-" if v is None else str(v)

    out += ["## Runs", "", "| Run | Repos | PRs (facts/upkeep/issue) | Open | Merged | Failing | Needs you | Untriaged | Fixed | "
            "False positive | Accepted risk | Minutes | Spend |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        untriaged = "-" if r["findings_before"] is None else f"{r['findings_before']} → {r['findings_after']}"
        spend = "-" if r["spend_usd"] is None else f"US${r['spend_usd']:.2f}"
        if r["spend_usd"] is not None and r["spend_unknown_prs"]:
            spend += f" + {r['spend_unknown_prs']} unknown"
        kinds = "/".join(str(r.get("prs_" + k, 0)) for k in KINDS)
        out.append(f"| {r['run']} | {r['repos']} | {r['prs']} ({kinds}) | {r['prs_open']} | {r['prs_merged']} | "
                   f"{r['prs_failing']} | {r['needs_you_open']} | {untriaged} | {r['fixed']} | "
                   f"{r['false_positive']} | {r['accepted_risk']} | {num(r['minutes'])} | {spend} |")
    out += ["", "## Open PRs", ""]
    if open_prs:
        groups = [("Facts", ("facts",)), ("Upkeep", ("upkeep",)), ("Issues", ("issue",)),
                  ("Other", tuple({p["kind"] for p in open_prs} - set(KINDS)))]
        for heading, kinds in groups:
            group = [p for p in open_prs if p["kind"] in kinds]
            if not group:
                continue
            out += [f"### {heading}", "", "| Repo | PR | Title | CI | Review | Waiting (days) |",
                    "|---|---|---|---|---|---|"]
            for p in sorted(group, key=lambda p: (-(p["waiting_days"] or 0), p["repo"].lower(), p["url"])):
                link = f"[#{p['number']}]({p['url']})" if p["number"] else f"preview ({p['url']})"
                title = p["title"].replace("|", "\\|")
                out.append(f"| {p['repo']} | {link} | {title} | {p['ci']} | "
                           f"{p['review'] or '-'} | {num(p['waiting_days'])} |")
            out.append("")
        out.pop()
    else:
        out.append("- None.")
    out += ["", "## Account-wide", ""]
    out += [f"- {g}" for g in data.get("account") or []] or ["- None."]
    out += ["", "## Needs you", ""]
    out += [f"- {i['repo']} " + (f"[#{i['number']}]({i['url']})" if i["number"] else "preview")
            + f": {i['item']}" for i in open_items] or ["- None."]
    out += ["", "## Holds", ""]
    out += [f"- {h['repo']}: {h['hold']}" for h in data["holds"] if h["current"]] or ["- None."]
    if data.get("comments"):
        out += ["", "## Would comment", ""]
        for c in data["comments"]:
            first = next((line for line in c["text"].splitlines() if line.strip() and line.strip() != "---"), "")
            out.append(f"- {c['repo']}" + (f" #{c['issue']}" if c.get("issue") else "") + f": {first}")
    if data.get("notes"):
        out += ["", "## No PR", ""]
        for c in data["notes"]:
            first = next((line for line in c["text"].splitlines() if line.strip() and line.strip() != "---"), "")
            out.append(f"- {c['repo']}: {first}")
    if data.get("preview_problems"):
        out += ["", "## Preview problems", ""]
        out += [f"- {p['file']}: {p['problem']}" for p in data["preview_problems"]]
    if repos is not None:
        out += ["", "## Opted out", ""]
        out += [f"- {r['repo']}: {r.get('opt_out_line') or 'Sadr: off'}"
                for r in repos["repos"] if r["opted_out"]] or ["- None."]
        claimed = [r["repo"] for r in repos["repos"] if r.get("recently_claimed")]
        if claimed:
            out += ["", "## Claimed by another run", ""] + [f"- {r}" for r in claimed]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--org", action="append", default=[], help="an organization to report on (repeat for more)")
    ap.add_argument("--user", action="append", default=[], help="a user to report on (repeat for more)")
    ap.add_argument("--repo", action="append", default=[], help="one repo as OWNER/NAME (repeat for more)")
    ap.add_argument("--repos", metavar="FILE", help="repos.py --json output, to list opted-out repos")
    ap.add_argument("--bodies", metavar="DIR", help="read preview PR bodies from DIR/OWNER__NAME/*.md")
    ap.add_argument("--all-bodies", action="store_true",
                    help="with --bodies, read every repo's files, not only the repos in scope")
    ap.add_argument("--gaps", metavar="FILE", action="append", default=[],
                    help="account-wide gaps: a JSON list of strings, or one '- ' bullet per line (repeat for more)")
    ap.add_argument("--new-preview-dir", nargs="?", const="", metavar="BASE",
                    help="create a new, unique preview folder (under BASE if given), print its path and exit")
    ap.add_argument("--legacy-prefix", action="append", default=[], metavar="PREFIX",
                    help="also read PRs from branches that start with PREFIX, such as factory/ (repeat for more)")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--json", action="store_true", help="same as --format json")
    ap.add_argument("--out", metavar="DIR", help="write files to DIR instead of printing")
    a = ap.parse_args(argv)
    if a.new_preview_dir is not None:
        print(new_preview_dir(a.new_preview_dir or None))
        return 0
    if not (a.org or a.user or a.repo or a.bodies):
        ap.error("pass at least one --org, --user, --repo or --bodies")
    if a.bodies and not os.path.isdir(a.bodies):
        ap.error(f"--bodies: no such folder: {a.bodies}")
    fmt = "json" if a.json else a.format
    quals = [f"org:{o}" for o in a.org] + [f"user:{u}" for u in a.user] + [f"repo:{r}" for r in a.repo]
    at = _gh.now()
    try:
        nodes = [n for q in quals for n in search_all(q, legacy=a.legacy_prefix)]
    except (IncompleteSearch, _gh.GhError) as e:
        print(f"report.py: {e}", file=sys.stderr)
        return 2
    repos = None
    if a.repos:
        with open(a.repos, encoding="utf-8") as f:
            repos = json.load(f)
    gaps = []
    for path in a.gaps:
        gaps += read_gaps(path)
    comments, notes, problems = [], [], []
    if a.bodies:
        scope = None
        if not a.all_bodies:
            scope = {x.lower() for x in a.org + a.user + a.repo}
            scope |= {r["repo"].lower() for r in (repos or {}).get("repos", [])}
            if not scope:
                ap.error("--bodies reads only the repos in scope. Pass --repo, --org, --user or --repos, "
                         "or --all-bodies")
        preview, comments, notes, problems = read_bodies(a.bodies, scope)
        nodes += preview
        if os.path.exists(os.path.join(a.bodies, GAPS_FILE)):
            gaps += read_gaps(os.path.join(a.bodies, GAPS_FILE))
    data = build(nodes, at, a.legacy_prefix, gaps)
    if a.bodies:
        data["comments"] = comments
        data["notes"] = notes
        data["preview_problems"] = problems
    if fmt == "json":
        data = {"generated_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), **data}
        if repos is not None:
            data["opted_out"] = [{"repo": r["repo"], "line": r.get("opt_out_line")}
                                 for r in repos["repos"] if r["opted_out"]]
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        if fmt == "md":
            path = os.path.join(a.out, "report.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(render_md(data, at, repos))
            print(path)
        else:
            for name, rows in data.items():
                if name == "generated_at":
                    continue
                path = os.path.join(a.out, f"{name}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=1, ensure_ascii=False)
                print(f"{name}: {len(rows)} rows -> {path}")
    elif fmt == "md":
        sys.stdout.write(render_md(data, at, repos))
    else:
        json.dump(data, sys.stdout, indent=1, ensure_ascii=False)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
