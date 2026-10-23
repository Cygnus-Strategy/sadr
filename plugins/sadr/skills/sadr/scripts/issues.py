#!/usr/bin/env python3
"""List the enhancement issues that Sadr may build in one repo.

Issue text is untrusted input. Sadr takes an open issue only when all of
these are true:

- A user with write, maintain or admin permission on the repo applied the
  label. The script reads the issue timeline to find who applied it.
- No open PR is linked to the issue. A linked PR closes the issue by
  reference, or comes from the branch sadr/issue-<N> or
  sadr/issue-<N>-<part>.
- Sadr did not ask a question on the issue that is still unanswered. Sadr's
  questions end with "Asked by [Sadr](https://sadr.cygnusstrategy.com), a dark factory for software." A later
  comment by someone else, or a later edit, answers it.
- The newest comment by a user with write access does not ask another
  user for approval that has no reply yet. The script reports such an
  issue as skipped with pending_signoff.

For each eligible issue the script also reports what may already deliver
it, so that Sadr can comment instead of building it twice:

- merged_prs: merged PRs that close the issue, mention it, come from a
  branch that names its number (such as 272-calendar or issue-19), or
  share at least two title words with it (match "title", a guess to check)
- branches: branches in the repo that name the issue number
- parts: a hint that the issue plans several PRs (a task list, or text
  such as "one PR per page"). Build the next part and write "Part of #N."

When the repo has issues turned off, the script says so and lists nothing.

Examples:
  issues.py --repo acme/app
  issues.py --repo acme/app --label feature --limit 1 --json

Needs an authenticated `gh` CLI. Standard library only.
"""
import argparse
import json
import re
import sys
import urllib.parse

import _gh

WRITE_ROLES = {"admin", "maintain", "write"}
ASK_FOOTER = "asked by sadr, a dark factory for software."
# Sadr's own comment updates the issue. Count a later update as an edit only past this margin.
EDIT_MARGIN_SECONDS = 120
CLOSING_PRS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
        nodes { number url state title headRefName mergedAt }
      }
    }
  }
}"""
SADR_BRANCH = re.compile(r"sadr/issue-(\d+)(?:-[\w.-]+)?")
ASK_WORDS = re.compile(
    r"\b(approve|approval|sign[- ]?off|look(s)? good|ok(ay)? (with|to)|lgtm|let me know|thoughts|"
    r"what do you think|good to (go|implement)|green light|go ahead|any objections|confirm|review)\b", re.I)
MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")
PARTS_TEXT = re.compile(
    r"\b(one PR (per|for each) \w+|(in|as|across) (\w+ )?(several|multiple|separate|\d+|two|three|four|five) "
    r"(PRs|pull requests)|part \d+ of \d+|phase \d+)\b", re.I)
TASK = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+", re.M)
STOP = set("""about above after again also always another because been before being below between both but
cannot could does doing done each every from have having here instead into just like make more most much must
only other over same should since some such than that their them then there these they this those through under
until when where which while will with within without would your what allow allows adds added using uses used
make makes made support supports""".split())


def labeled_by(timeline, label):
    """Return the login that last applied LABEL, or None.

    The last 'labeled' event for the label is the one that counts. An
    earlier one may have been undone.
    """
    who = None
    for e in timeline or []:
        if e.get("event") == "labeled" and ((e.get("label") or {}).get("name") or "").lower() == label.lower():
            who = (e.get("actor") or {}).get("login")
    return who


def pending_question(timeline, issue_updated_at=None):
    """Return the time of Sadr's unanswered question on an issue, or None.

    The question is answered when someone else comments after it, or when
    the issue was updated well after it.
    """
    asked, asker = None, None
    for e in timeline or []:
        if e.get("event") != "commented":
            continue
        # The sign-off links "Sadr" to the landing page; compare its text only.
        body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", (e.get("body") or "").strip()).lower()
        login = (e.get("actor") or e.get("user") or {}).get("login")
        if body.endswith(ASK_FOOTER):
            asked, asker = e.get("created_at"), login
        elif asked and login != asker:
            asked = None
    if asked and issue_updated_at:
        gap = (_gh.parse_time(issue_updated_at) - _gh.parse_time(asked)).total_seconds()
        if gap > EDIT_MARGIN_SECONDS:
            return None
    return asked


def can_write(perm):
    """True when a collaborator permission response grants write or more.

    `role_name` names the exact role. The older `permission` field maps
    maintain to write and triage to read.
    """
    if not perm:
        return False
    role = perm.get("role_name") or perm.get("permission") or ""
    return role.lower() in WRITE_ROLES


def branch_names_issue(branch, number):
    """True when a branch name names the issue: 272-calendar, issue-19, feature/19-x, sadr/issue-19."""
    b = branch or ""
    return (re.search(rf"(^|/)#?{number}([/_.-]|$)", b) is not None
            or re.search(rf"(^|[/_-])(issue|issues|gh)[-_]?#?{number}([/_.-]|$)", b, re.I) is not None)


def words(text, drop=()):
    """The set of significant title words, without a plural s."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 4 or w in STOP or w in drop or w.isdigit():
            continue
        out.add(w[:-1] if w.endswith("s") and not w.endswith("ss") else w)
    return out


def parts_hint(body):
    """A hint that an issue plans several PRs, or None."""
    tasks = TASK.findall(body or "")
    if len(tasks) >= 2:
        return {"source": "task list", "total": len(tasks), "done": sum(t.lower() == "x" for t in tasks)}
    m = PARTS_TEXT.search(body or "")
    if m:
        return {"source": "text", "phrase": m.group(0)}
    return None


def pending_signoff(comments, can_write_login):
    """Return {by, waiting_on, at} when the newest write-access comment awaits another user's approval.

    COMMENTS are timeline "commented" events in order. The ask is answered
    when a user it mentions comments later.
    """
    writers = [c for c in comments if can_write_login(login_of(c))]
    if not writers:
        return None
    last = writers[-1]
    body = last.get("body") or ""
    who = login_of(last)
    mentioned = [m for m in MENTION.findall(body) if m.lower() != (who or "").lower() and "sadr" not in m.lower()]
    if not mentioned or not ASK_WORDS.search(body):
        return None
    later = comments[comments.index(last) + 1:]
    if any((login_of(c) or "").lower() in (m.lower() for m in mentioned) for c in later):
        return None
    return {"by": who, "waiting_on": mentioned, "at": last.get("created_at")}


def login_of(event):
    return ((event.get("actor") or event.get("user") or {}).get("login"))


class Checker:
    def __init__(self, repo):
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self._perms = {}
        self._closed = None
        self._branches = None
        pulls = _gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) or []
        self.branch_prs = {}
        for p in pulls:
            m = SADR_BRANCH.fullmatch((p.get("head") or {}).get("ref") or "")
            if m:
                self.branch_prs[int(m.group(1))] = p["html_url"]

    def merged_pulls(self):
        """Merged PRs of the repo, read once."""
        if self._closed is None:
            closed = _gh.api(f"repos/{self.repo}/pulls?state=closed&per_page=100", paginate=True) or []
            self._closed = [p for p in closed if p.get("merged_at")]
        return self._closed

    def branches(self):
        if self._branches is None:
            self._branches = [b["name"] for b in _gh.api(f"repos/{self.repo}/branches?per_page=100",
                                                         paginate=True) or []]
        return self._branches

    def can_write_login(self, login):
        return bool(login) and not login.endswith("[bot]") and can_write(self.permission(login))

    def permission(self, login):
        if login not in self._perms:
            self._perms[login] = _gh.api(f"repos/{self.repo}/collaborators/{login}/permission")
        return self._perms[login]

    def closing_prs(self, number):
        data = _gh.graphql(CLOSING_PRS, owner=self.owner, name=self.name, number=number)
        issue = ((data.get("repository") or {}).get("issue")) or {}
        return ((issue.get("closedByPullRequestsReferences") or {}).get("nodes")) or []

    def open_linked_pr(self, number, closing=None):
        """Return the URL of an open PR linked to the issue, or None."""
        if number in self.branch_prs:
            return self.branch_prs[number]
        for pr in closing if closing is not None else self.closing_prs(number):
            if pr.get("state") == "OPEN":
                return pr.get("url")
        return None

    def delivered(self, issue, timeline, closing):
        """Merged PRs and branches that may deliver ISSUE. See the module docstring."""
        n, found = issue["number"], {}

        def add(num, url, title, branch, merged_at, match):
            if num not in found:
                found[num] = {"number": num, "url": url, "title": title, "branch": branch,
                              "merged_at": merged_at, "match": match}

        for pr in closing:
            if pr.get("state") == "MERGED":
                add(pr["number"], pr.get("url"), pr.get("title"), pr.get("headRefName"), pr.get("mergedAt"), "closes")
        for e in timeline or []:
            src = (e.get("source") or {}).get("issue") or {}
            pr = src.get("pull_request") or {}
            same_repo = f"github.com/{self.repo}/".lower() in (src.get("html_url") or "").lower()
            if e.get("event") == "cross-referenced" and pr.get("merged_at") and same_repo:
                add(src["number"], src.get("html_url"), src.get("title"), None, pr["merged_at"], "mentions")
        drop = words(self.name)
        mine = words(issue.get("title"), drop)
        guesses = []
        for p in self.merged_pulls():
            branch = (p.get("head") or {}).get("ref")
            if branch_names_issue(branch, n):
                add(p["number"], p["html_url"], p.get("title"), branch, p["merged_at"], "branch")
            elif p["merged_at"] >= (issue.get("created_at") or ""):
                shared = mine & words(p.get("title"), drop)
                if len(shared) >= 2:
                    guesses.append((len(shared), p, sorted(shared)))
        for _, p, shared in sorted(guesses, key=lambda g: (-g[0], -g[1]["number"]))[:3]:
            if p["number"] not in found:
                add(p["number"], p["html_url"], p.get("title"), (p.get("head") or {}).get("ref"), p["merged_at"],
                    "title")
                found[p["number"]]["shared_words"] = shared
        branches = [b for b in self.branches() if branch_names_issue(b, n)]
        return sorted(found.values(), key=lambda r: r["number"]), branches

    def judge(self, issue, label):
        """Return (row, None) for an eligible issue, or (None, skipped row)."""
        n = issue["number"]
        timeline = _gh.api(f"repos/{self.repo}/issues/{n}/timeline?per_page=100", paginate=True)
        skip = {"number": n, "url": issue["html_url"], "pending_signoff": False}
        who = labeled_by(timeline, label)
        if not who:
            return None, {**skip, "reason": f"no '{label}' labeled event in the timeline"}
        if not can_write(self.permission(who)):
            return None, {**skip, "reason": f"labeled by {who}, who has no write access"}
        asked = pending_question(timeline, issue.get("updated_at"))
        if asked:
            return None, {**skip, "reason": f"Sadr asked a question at {asked} and the issue has not changed since"}
        comments = [e for e in timeline or [] if e.get("event") == "commented"]
        signoff = pending_signoff(comments, self.can_write_login)
        if signoff:
            return None, {**skip, "pending_signoff": True, "signoff": signoff,
                          "reason": f"{signoff['by']} asked {', '.join('@' + w for w in signoff['waiting_on'])} "
                                    f"for approval at {signoff['at']}, with no reply yet"}
        closing = self.closing_prs(n)
        pr = self.open_linked_pr(n, closing)
        if pr:
            return None, {**skip, "reason": f"open PR {pr}"}
        merged, branches = self.delivered(issue, timeline, closing)
        return {"number": n, "title": issue["title"], "url": issue["html_url"], "labeled_by": who,
                "merged_prs": merged, "branches": branches, "parts": parts_hint(issue.get("body")),
                "pending_signoff": False}, None


def eligible(repo, label="enhancement", limit=0):
    """Return (eligible issues, skipped issues with reasons, disabled reason) for REPO.

    LIMIT caps the eligible issues, lowest number first. 0 means no cap.
    """
    info = _gh.api(f"repos/{repo}")
    if info is None:
        raise _gh.GhError(f"repo {repo} not found, or the token cannot read it")
    if not info.get("has_issues", True):
        return [], [], "issues are turned off in the repo settings"
    issues = _gh.api(f"repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=100",
                     paginate=True) or []
    checker = Checker(repo)
    ok, skipped = [], []
    for issue in sorted(issues, key=lambda i: i["number"]):
        if "pull_request" in issue:
            continue
        if limit and len(ok) >= limit:
            skipped.append({"number": issue["number"], "url": issue["html_url"], "pending_signoff": None,
                            "reason": f"over --limit {limit}, not checked"})
            continue
        row, skip = checker.judge(issue, label)
        if row:
            ok.append(row)
        else:
            skipped.append(skip)
    return ok, skipped, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument("--label", default="enhancement", help="the label to look for (default: enhancement)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="list at most N eligible issues, lowest number first (default: all)")
    _gh.add_json(ap)
    a = ap.parse_args(argv)
    if "/" not in a.repo:
        ap.error("--repo takes OWNER/NAME")
    try:
        ok, skipped, disabled = eligible(a.repo, a.label, a.limit)
    except _gh.GhError as e:
        sys.exit(str(e))
    if a.json:
        json.dump({"repo": a.repo, "issues_disabled": disabled, "issues": ok, "skipped": skipped}, sys.stdout, indent=2)
        print()
        return
    if disabled:
        print(f"{a.repo}: {disabled}. No issues to build.", file=sys.stderr)
        return
    for r in ok:
        print(f"#{r['number']}\t{r['title']}\tlabeled by {r['labeled_by']}\t{r['url']}")
        for m in r["merged_prs"]:
            extra = f" (shared words: {', '.join(m['shared_words'])})" if m.get("shared_words") else ""
            print(f"  merged PR #{m['number']} ({m['match']}{extra}): {m['title']}, merged {m['merged_at'][:10]}")
        if r["branches"]:
            print(f"  branches: {', '.join(r['branches'])}")
        if r["parts"]:
            p = r["parts"]
            print(f"  parts: {p['done']} of {p['total']} tasks done" if p["source"] == "task list"
                  else f"  parts: the issue says \"{p['phrase']}\"")
    for s_ in skipped:
        print(f"skipped #{s_['number']}: {s_['reason']}", file=sys.stderr)
    print(f"{len(ok)} eligible, {len(skipped)} skipped.", file=sys.stderr)


if __name__ == "__main__":
    main()
