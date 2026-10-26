import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import NOW, fixture
import _gh
import report


def node(repo, number, branch, title, body, state="OPEN", created="2026-10-15T00:00:00Z",
         updated="2026-10-21T00:00:00Z", merged=None, ci="SUCCESS"):
    return {"number": number, "title": title, "body": body, "url": f"https://github.com/{repo}/pull/{number}",
            "state": state, "isDraft": False, "createdAt": created, "updatedAt": updated, "mergedAt": merged,
            "closedAt": merged, "headRefName": branch, "reviewDecision": "REVIEW_REQUIRED",
            "repository": {"nameWithOwner": repo},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci} if ci else None}}]}}


def nodes():
    return [
        node("acme/app", 41, "sadr/upkeep", "sadr: upkeep 2026-10-21", fixture("pr_upkeep.md"), ci="FAILURE"),
        node("acme/app", 43, "sadr/issue-12", "sadr: #12 Add CSV export", fixture("pr_feature.md"),
             state="MERGED", merged="2026-10-21T15:00:00Z"),
        node("acme/lib", 7, "sadr/upkeep", "sadr: upkeep 2026-10-14", fixture("pr_legacy.md"), state="CLOSED",
             updated="2026-10-14T00:00:00Z", ci=None),
        # Not Sadr's: wrong title, and wrong branch.
        node("acme/lib", 8, "sadr/upkeep", "Bump deps", ""),
        node("acme/lib", 9, "feature/x", "sadr: lookalike", ""),
    ]


class ParseTest(unittest.TestCase):
    def test_sections(self):
        sec = report.sections(fixture("pr_upkeep.md"))
        self.assertEqual(len(sec["holds"]), 2)
        self.assertIn("Cloudflare Python Workers support only 3.14. Revisit 2026-11-16.", sec["holds"][0])
        self.assertEqual(sec["not done or not checked"], [])
        self.assertNotIn("Opened by Sadr", json.dumps(sec))
        self.assertIn("security findings", report.sections(fixture("pr_legacy.md")))

    def test_finding_counts(self):
        c = report.finding_counts(report.sections(fixture("pr_upkeep.md"))["security findings"])
        self.assertEqual(c, {"before": 4, "after": 0, "fixed": 3, "false_positive": 1, "accepted_risk": 0})
        legacy = report.finding_counts(report.sections(fixture("pr_legacy.md"))["security findings"])
        self.assertEqual((legacy["before"], legacy["fixed"], legacy["false_positive"]), (None, 1, 2))
        self.assertEqual(report.finding_counts(["untriaged: 2 -> 1"])["after"], 1)

    def test_run_usage(self):
        self.assertEqual(report.run_usage(["1 hour 41 minutes, US$1,206.20"]), (101, 1206.2, "api"))
        self.assertEqual(report.run_usage(["23 minutes, subscription"]), (23, None, "subscription"))
        self.assertEqual(report.run_usage([]), (None, None, None))


class BuildTest(unittest.TestCase):
    def test_build(self):
        data = report.build(nodes(), NOW)
        prs = {p["number"]: p for p in data["prs"]}
        self.assertEqual(sorted(prs), [7, 41, 43])
        self.assertEqual(prs[41]["run"], "2026-10-21")
        self.assertEqual(prs[41]["kind"], "upkeep")
        self.assertEqual(prs[41]["ci"], "fail")
        self.assertEqual(prs[41]["waiting_days"], 7.5)
        self.assertEqual(prs[43]["kind"], "issue")
        self.assertEqual(prs[43]["issue"], 12)
        self.assertEqual(prs[43]["run"], "2026-10-21")  # the merge date
        self.assertIsNone(prs[43]["waiting_days"])
        self.assertEqual(prs[7]["ci"], "none")

        items = {(i["number"], i["item"][:12]): i for i in data["items"]}
        self.assertEqual(len(items), 4)
        self.assertEqual(items[(41, "Add the SEMG")]["status"], "open")
        self.assertEqual(items[(41, "Done: Turn o")]["resolution"], "marked done")
        self.assertEqual(items[(7, "Rotate the l")]["resolution"], "PR closed")

        self.assertEqual([h["current"] for h in data["holds"]], [True, True])

        run = data["runs"][0]
        self.assertEqual(run["run"], "2026-10-21")
        self.assertEqual((run["prs"], run["prs_open"], run["prs_merged"], run["prs_failing"]), (2, 1, 1, 1))
        self.assertEqual(run["needs_you_open"], 2)
        self.assertEqual((run["findings_before"], run["findings_after"]), (4, 0))
        self.assertEqual((run["fixed"], run["false_positive"], run["accepted_risk"]), (3, 1, 1))
        self.assertEqual((run["minutes"], run["spend_usd"]), (124, 1206.2))
        self.assertEqual(data["runs"][1]["run"], "2026-10-14")

    def test_render_md(self):
        data = report.build(nodes(), NOW)
        repos = {"repos": [{"repo": "acme/app", "opted_out": False},
                           {"repo": "acme/old", "opted_out": True, "opt_out_line": "Sadr: off. Rewrite."}]}
        md = report.render_md(data, NOW, repos)
        self.assertIn("3 Sadr PRs in 2 runs. 1 open, 2 items need you. 2 repos in scope, 1 opted out.", md)
        self.assertIn("| 2026-10-21 | 1 | 2 (0/1/1) | 1 | 1 | 1 | 2 | 4 → 0 | 3 | 1 | 1 | 124 | US$1206.20 |", md)
        self.assertIn("- acme/old: Sadr: off. Rewrite.", md)
        self.assertNotIn("Done: Turn on", md)


def page(total, items, cursor=None):
    return {"search": {"issueCount": total, "nodes": items,
                       "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}


class SearchTest(unittest.TestCase):
    def test_paginates(self):
        a, b = nodes()[:2]
        calls = []

        def graphql(query, q, after):
            calls.append((q, after))
            return page(2, [a], "c1") if after is None else page(2, [b])

        with mock.patch.object(_gh, "graphql", graphql):
            got = report.search_one("q", 4, lambda s: None)
        self.assertEqual(got, [a, b])
        self.assertEqual(calls, [("q", None), ("q", "c1")])

    def test_union_of_branch_and_title_searches(self):
        a, b, c = nodes()[:3]
        qs = []

        def graphql(query, q, after):
            qs.append(q)
            return page(2, [a, b]) if "head:sadr/" in q else page(2, [b, c])

        with mock.patch.object(_gh, "graphql", graphql):
            got = report.search_all("org:acme", sleep=lambda s: None)
        self.assertEqual([n["number"] for n in got], [41, 43, 7])
        self.assertEqual(qs, ["org:acme is:pr head:sadr/ sort:created-asc",
                              'org:acme is:pr in:title "sadr:" sort:created-asc'])

    def test_empty_results_search_again_once(self):
        a = nodes()[0]
        results = iter([page(0, []), page(0, []), page(1, [a]), page(0, [])])
        sleeps = []
        with mock.patch.object(_gh, "graphql", lambda query, q, after: next(results)):
            got = report.search_all("org:acme", sleep=sleeps.append)
        self.assertEqual(got, [a])
        self.assertEqual(sleeps, [10])

    def test_retries_short_results_then_succeeds(self):
        a, b = nodes()[:2]
        results = iter([page(2, [a]), page(2, [a, b])])
        sleeps = []
        with mock.patch.object(_gh, "graphql", lambda query, q, after: next(results)):
            got = report.search_one("q", 4, sleeps.append)
        self.assertEqual(len(got), 2)
        self.assertEqual(sleeps, [20])

    def test_gives_up_when_still_short(self):
        with mock.patch.object(_gh, "graphql", lambda query, q, after: page(3, [nodes()[0]])):
            with self.assertRaises(report.IncompleteSearch):
                report.search_all("org:acme", attempts=3, sleep=lambda s: None)

    def test_errors_count_as_short(self):
        def boom(query, q, after):
            raise _gh.GhError("secondary rate limit")
        with mock.patch.object(_gh, "graphql", boom):
            with self.assertRaises(report.IncompleteSearch):
                report.search_one("q", 2, lambda s: None)

    def test_main_exits_2_when_short_and_writes_json(self):
        with mock.patch.object(report, "search_all", side_effect=report.IncompleteSearch("1 of 2")), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(report.main(["--org", "acme"]), 2)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(report, "search_all", return_value=nodes()), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(report.main(["--user", "acme", "--json", "--out", d]), 0)
            self.assertEqual(sorted(os.listdir(d)),
                             ["account.json", "findings.json", "holds.json", "items.json", "prs.json", "runs.json"])
            with open(os.path.join(d, "prs.json")) as f:
                self.assertEqual(len(json.load(f)), 3)


def dated_bodies(test_class, name):
    """Copy fixtures/NAME to a temporary folder with every file dated NOW.

    read_bodies dates a preview body by its file time, and a checkout sets
    that to the time of the checkout. A run writes its bodies on its own day.
    """
    folder = tempfile.mkdtemp()
    test_class.addClassCleanup(shutil.rmtree, folder)
    copy = os.path.join(folder, name)
    shutil.copytree(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", name), copy)
    stamp = NOW.timestamp()
    for root, _, files in os.walk(copy):
        for f in files:
            os.utime(os.path.join(root, f), (stamp, stamp))
    return copy


class PreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.BODIES = dated_bodies(cls, "bodies")

    def test_read_bodies(self):
        nodes, comments, notes, problems = report.read_bodies(self.BODIES)
        self.assertEqual((notes, problems), ([], []))
        self.assertEqual([(n["repository"]["nameWithOwner"], n["headRefName"], n["state"]) for n in nodes],
                         [("acme/app", "sadr/issue-12-export", "PREVIEW"), ("acme/app", "sadr/upkeep", "PREVIEW")])
        self.assertEqual(nodes[1]["url"], os.path.join("acme__app", "upkeep.md"))
        self.assertEqual((comments[0]["repo"], comments[0]["issue"]), ("acme/lib", 7))

    def test_preview_report_keeps_unknown_spend_null(self):
        nodes, comments, _, _ = report.read_bodies(self.BODIES)
        data = report.build(nodes, NOW)
        run = data["runs"][0]
        self.assertEqual((run["run"], run["prs"], run["prs_open"], run["prs_preview"]), ("2026-10-22", 2, 2, 2))
        self.assertIsNone(run["spend_usd"])
        self.assertEqual(run["minutes"], 124)
        self.assertEqual(run["spend_unknown_prs"], 1)
        # Needs-you items of a preview PR stay open.
        self.assertEqual([i["status"] for i in data["items"]], ["open", "done", "open"])
        feature = [p for p in data["prs"] if p["kind"] == "issue"][0]
        self.assertEqual(feature["issue"], 12)
        data["comments"] = comments
        md = report.render_md(data, NOW)
        self.assertIn("| 124 | - |", md)
        self.assertIn("preview (acme__app/upkeep.md)", md)
        self.assertIn("## Would comment", md)
        self.assertIn("- acme/lib #7: No PR. Sadr would post this comment", md)

    def test_main_with_bodies_only(self):
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(report.main(["--bodies", self.BODIES, "--all-bodies", "--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertIsNone(data["runs"][0]["spend_usd"])
        self.assertEqual(len(data["comments"]), 1)

    def test_bodies_need_a_scope(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            report.main(["--bodies", self.BODIES])

    def test_known_and_unknown_spend(self):
        prs = [{"run": "r", "repo": "a", "state": "OPEN", "ci": "pass", "minutes": None, "spend_usd": 2.5,
                "billing": "api"},
               {"run": "r", "repo": "b", "state": "OPEN", "ci": "pass", "minutes": None, "spend_usd": None,
                "billing": None}]
        run = report.runs_from(prs, [], [])[0]
        self.assertEqual((run["spend_usd"], run["spend_unknown_prs"], run["minutes"]), (2.5, 1, None))


class PreviewKindsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.BODIES = dated_bodies(cls, "bodies2")

    def read(self, scope=frozenset({"acme"})):
        return report.read_bodies(self.BODIES, scope)

    def test_scope_titles_and_kinds(self):
        nodes, comments, notes, problems = self.read()
        got = [(n["repository"]["nameWithOwner"], n["headRefName"], n["title"]) for n in nodes]
        self.assertEqual(got[:2], [("acme/app", "sadr/facts", "sadr: AGENTS.md facts 2026-10-22"),
                                   ("acme/app", "sadr/upkeep", "sadr: upkeep 2026-10-22")])
        self.assertEqual(got[2][:2], ("acme/lib", "sadr/issue-9-cli"))
        self.assertTrue(got[2][2].startswith("sadr: #9"))
        self.assertNotIn("other/web", {n["repository"]["nameWithOwner"] for n in nodes})
        self.assertEqual([(c["repo"], c["issue"]) for c in comments], [("acme/lib", 11)])
        self.assertEqual([n["file"] for n in notes], [os.path.join("acme__lib", "no-pr.md")])
        self.assertEqual(len(problems), 1)
        self.assertIn("no PR title", problems[0]["problem"])
        # The untitled PR keeps its first heading in the body.
        self.assertTrue(nodes[2]["body"].startswith("## What this PR does"))

    def test_scope_by_repo(self):
        nodes, _, _, _ = self.read({"other/web"})
        self.assertEqual([n["repository"]["nameWithOwner"] for n in nodes], ["other/web"])
        self.assertEqual(len(report.read_bodies(self.BODIES)[0]), 4)

    def test_kinds_and_account_gaps(self):
        nodes, *_ = self.read()
        gaps = report.read_gaps(os.path.join(self.BODIES, "account-gaps.md"))
        data = report.build(nodes, NOW, gaps=gaps)
        self.assertEqual(sorted(p["kind"] for p in data["prs"]), ["facts", "issue", "upkeep"])
        run = data["runs"][0]
        self.assertEqual((run["prs_facts"], run["prs_upkeep"], run["prs_issue"]), (1, 1, 1))
        # Listed once, and gone from the per-PR items.
        self.assertEqual(data["account"], ["Turn on Semgrep Secrets for the acme organization.",
                                           "Give the Sadr App the Workflows permission."])
        self.assertEqual([i["item"] for i in data["items"]], ["Review the Node 26 move."])
        # Holds stay current although the facts PR has none.
        self.assertTrue(all(h["current"] for h in data["holds"]))
        md = report.render_md(data, NOW)
        self.assertIn("### Facts", md)
        self.assertIn("### Upkeep", md)
        self.assertIn("### Issues", md)
        self.assertEqual(md.count("Semgrep Secrets"), 1)
        self.assertIn("| 3 (1/1/1) |", md)

    def test_main_reads_gaps_file_and_notes(self):
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(report.main(["--bodies", self.BODIES, "--all-bodies"]), 0)
        md = out.getvalue()
        self.assertIn("## Account-wide", md)
        self.assertIn("## No PR", md)
        self.assertIn("## Preview problems", md)
        self.assertIn("other/web", md)

    def test_gaps_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(["Turn on X", "turn on x."], f)
        try:
            self.assertEqual(report.unique_gaps(report.read_gaps(f.name)), ["Turn on X"])
        finally:
            os.unlink(f.name)

    def test_new_preview_dir_is_unique(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "77"}):
            a = report.new_preview_dir(base)
            b = report.new_preview_dir(base)
            self.assertNotEqual(a, b)
            self.assertTrue(os.path.isdir(a) and not os.listdir(a))
            self.assertIn("-77-1-", os.path.basename(a))
            with mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(report.main(["--new-preview-dir", base]), 0)
            self.assertTrue(os.path.isdir(out.getvalue().strip()))


class LegacyTest(unittest.TestCase):
    def test_legacy_prefix(self):
        old = node("acme/app", 21, "factory/2026-10-19", "factory: weekly upgrades 2026-10-19", "Body", state="MERGED",
                   merged="2026-10-19T19:38:17Z")
        self.assertEqual(report.build([old], NOW)["prs"], [])
        data = report.build([old], NOW, legacy=["factory/"])
        self.assertEqual((data["prs"][0]["kind"], data["prs"][0]["run"]), ("legacy", "2026-10-19"))
        self.assertIn("org:acme is:pr head:factory/ sort:created-asc", report.search_queries("org:acme", ["factory/"]))


if __name__ == "__main__":
    unittest.main()
