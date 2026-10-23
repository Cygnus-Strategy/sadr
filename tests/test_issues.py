import unittest
from unittest import mock

from helpers import FakeApi, fixture_json
import _gh
import issues

FOOTER = "Asked by [Sadr](https://sadr.cygnusstrategy.com), a dark factory for software."


def labeled(login, name="sadr"):
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": login}}


def comment(login, body, at):
    return {"event": "commented", "actor": {"login": login}, "body": body, "created_at": at}


def issue(n, title="Feature", updated="2026-10-20T00:00:00Z"):
    return {"number": n, "title": title, "html_url": f"https://github.com/acme/app/issues/{n}", "updated_at": updated}


class PureTest(unittest.TestCase):
    def test_last_labeled_event_counts(self):
        tl = [labeled("maint"), {"event": "unlabeled", "label": {"name": "sadr"}, "actor": {"login": "maint"}},
              labeled("stranger"), labeled("maint", "bug")]
        self.assertEqual(issues.labeled_by(tl, "Sadr"), "stranger")
        self.assertIsNone(issues.labeled_by([labeled("x", "bug")], "sadr"))

    def test_can_write(self):
        self.assertTrue(issues.can_write({"permission": "write", "role_name": "maintain"}))
        self.assertTrue(issues.can_write({"permission": "admin"}))
        self.assertFalse(issues.can_write({"permission": "read", "role_name": "triage"}))
        self.assertFalse(issues.can_write(None))

    def test_pending_question(self):
        asked = [comment("sadr[bot]", "What format?\n\n" + FOOTER, "2026-10-20T00:00:00Z")]
        self.assertEqual(issues.pending_question(asked, "2026-10-20T00:00:30Z"), "2026-10-20T00:00:00Z")
        answered = asked + [comment("maint", "CSV.", "2026-10-21T00:00:00Z")]
        self.assertIsNone(issues.pending_question(answered, "2026-10-21T00:00:00Z"))
        self.assertIsNone(issues.pending_question(asked, "2026-10-21T00:00:00Z"))  # edited later
        self.assertIsNone(issues.pending_question([], None))


class EligibleTest(unittest.TestCase):
    def test_eligible(self):
        closing = {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": []}}}}
        closing_open = {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [
            {"number": 9, "url": "https://github.com/acme/app/pull/9", "state": "OPEN"}]}}}}

        def graphql(query, owner, name, number):
            return closing_open if number == 4 else closing

        api = FakeApi({
            "repos/acme/app": {"full_name": "acme/app", "has_issues": True},
            "repos/acme/app/branches": [],
            "repos/acme/app/issues?": [issue(1), issue(2), issue(3), issue(4), issue(5), issue(6),
                                       {**issue(7), "pull_request": {}}],
            "repos/acme/app/pulls": [{"number": 50, "html_url": "https://github.com/acme/app/pull/50",
                                      "head": {"ref": "sadr/issue-3"}}],
            "repos/acme/app/issues/1/timeline": [labeled("maint")],
            "repos/acme/app/issues/2/timeline": [labeled("stranger")],
            "repos/acme/app/issues/3/timeline": [labeled("maint")],
            "repos/acme/app/issues/4/timeline": [labeled("maint")],
            "repos/acme/app/issues/5/timeline": [],
            "repos/acme/app/issues/6/timeline": [labeled("maint"),
                                                 comment("sadr[bot]", "Which page?\n" + FOOTER, "2026-10-20T00:00:00Z")],
            "repos/acme/app/collaborators/maint/permission": {"permission": "write", "role_name": "maintain"},
            "repos/acme/app/collaborators/stranger/permission": {"permission": "read", "role_name": "read"},
        })
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "graphql", graphql):
            ok, skipped, _ = issues.eligible("acme/app", "sadr")
        self.assertEqual(ok, [{"number": 1, "title": "Feature", "url": "https://github.com/acme/app/issues/1",
                               "labeled_by": "maint", "merged_prs": [], "branches": [], "parts": None,
                               "pending_signoff": False}])
        reasons = {s["number"]: s["reason"] for s in skipped}
        self.assertIn("no write access", reasons[2])
        self.assertIn("pull/50", reasons[3])
        self.assertIn("pull/9", reasons[4])
        self.assertIn("no 'sadr' labeled event", reasons[5])
        self.assertIn("asked a question", reasons[6])
        self.assertNotIn(7, reasons)
        # One permission lookup per user.
        self.assertEqual(sum("collaborators/maint" in c for c in api.calls), 1)

    def test_label_is_url_encoded(self):
        api = FakeApi({"repos/acme/app/pulls": [], "repos/acme/app": {"has_issues": True},
                       "repos/acme/app/issues": []})
        with mock.patch.object(_gh, "api", api):
            issues.eligible("acme/app", "good idea")
        self.assertIn("labels=good%20idea", api.calls[1])

    def test_default_label_is_sadr_and_cli_takes_one_issue(self):
        api = FakeApi({"repos/acme/app/pulls": [], "repos/acme/app": {"has_issues": True},
                       "repos/acme/app/issues": []})
        with mock.patch.object(_gh, "api", api):
            issues.eligible("acme/app")
        self.assertIn("labels=sadr&", api.calls[1])
        with mock.patch.object(issues, "eligible", return_value=([], [], None)) as el, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            issues.main(["--repo", "acme/app"])
        el.assert_called_once_with("acme/app", "sadr", 1)

    def test_issues_disabled(self):
        api = FakeApi({"repos/acme/app": {"has_issues": False}})
        with mock.patch.object(_gh, "api", api), mock.patch("sys.stdout") as out, \
                mock.patch("sys.stderr") as err:
            issues.main(["--repo", "acme/app"])
        self.assertIn("issues are turned off", "".join(c.args[0] for c in err.write.call_args_list))
        with mock.patch.object(_gh, "api", api):
            self.assertEqual(issues.eligible("acme/app"), ([], [], "issues are turned off in the repo settings"))

    def test_limit(self):
        api = FakeApi({
            "repos/acme/app": {"has_issues": True}, "repos/acme/app/branches": [], "repos/acme/app/pulls": [],
            "repos/acme/app/issues?": [issue(1), issue(2)],
            "repos/acme/app/issues/1/timeline": [labeled("maint")],
            "repos/acme/app/collaborators/maint/permission": {"role_name": "write"}})
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "graphql", lambda *a, **k: {}):
            ok, skipped, _ = issues.eligible("acme/app", limit=1)
        self.assertEqual(([r["number"] for r in ok], skipped[0]["number"]), ([1], 2))
        self.assertNotIn("repos/acme/app/issues/2/timeline", "".join(api.calls))


class DeliveredTest(unittest.TestCase):
    def test_merged_pr_found_by_title_words(self):
        # Recorded from a recorded case: issue #19 was delivered by #20, which never named the issue.
        api = FakeApi({
            "repos/acme/app": {"has_issues": True},
            "repos/acme/app/issues?": [fixture_json("issues_19.json")],
            "repos/acme/app/pulls?state=open": [],
            "repos/acme/app/pulls?state=closed": fixture_json("issues_closed_pulls.json"),
            "repos/acme/app/branches": [{"name": "main"}, {"name": "factory/2026-10-19"}],
            "repos/acme/app/issues/19/timeline": [labeled("maint")],
            "repos/acme/app/collaborators/maint/permission": {"role_name": "admin"}})
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "graphql", lambda *a, **k: {}):
            ok, _, _ = issues.eligible("acme/app")
        merged = ok[0]["merged_prs"]
        self.assertEqual([(m["number"], m["match"]) for m in merged], [(20, "title")])
        self.assertEqual(merged[0]["shared_words"], ["cloudflare", "worker"])
        # factory/2026-10-19 ends in 19, but it does not name the issue.
        self.assertEqual(ok[0]["branches"], [])

    def test_parts_branches_mentions_and_closing(self):
        # Recorded from the #272 case: one PR per page, five parts merged from 272-* branches.
        timeline = fixture_json("issues_parts_timeline.json")
        closing = {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [
            {"number": 278, "url": "https://github.com/acme/app/pull/278", "state": "MERGED",
             "title": "Add the dialog", "headRefName": "272-dialogs", "mergedAt": "2026-10-15T00:00:00Z"}]}}}}
        merged = [{"number": n, "html_url": f"https://github.com/acme/app/pull/{n}", "title": t,
                   "merged_at": "2026-10-15T12:00:00Z", "head": {"ref": b}}
                  for n, t, b in ((280, "Move Packing Lists to the shared dialog", "272-packing-lists-dialogs"),
                                  (290, "Unrelated", "1272-other"))]
        api = FakeApi({
            "repos/acme/app": {"has_issues": True},
            "repos/acme/app/issues?": [{**issue(272, "Replace confirm() and alert() with a dialog"),
                                        "created_at": "2026-10-09T19:19:31Z",
                                        "body": "Intro.\n\n### Rollout, one PR per page\n\n1. First."}],
            "repos/acme/app/pulls?state=open": [],
            "repos/acme/app/pulls?state=closed": merged,
            "repos/acme/app/branches": [{"name": "272-calendar-dialogs"}, {"name": "main"}],
            "repos/acme/app/issues/272/timeline": timeline,
            "repos/acme/app/collaborators/partner/permission": {"role_name": "write"}})
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "graphql", lambda *a, **k: closing):
            ok, _, _ = issues.eligible("acme/app")
        row = ok[0]
        got = {m["number"]: m["match"] for m in row["merged_prs"]}
        self.assertEqual(got[278], "closes")
        self.assertEqual(got[280], "mentions")
        self.assertEqual(sorted(got), [271, 278, 280, 282, 283, 284])
        self.assertEqual(row["branches"], ["272-calendar-dialogs"])
        self.assertEqual(row["parts"], {"source": "text", "phrase": "one PR per page"})

    def test_parts_from_task_list(self):
        self.assertEqual(issues.parts_hint("- [x] Tasks\n- [ ] Settings\n- [X] Shopping\n"),
                         {"source": "task list", "total": 3, "done": 2})
        self.assertIsNone(issues.parts_hint("Make the button blue."))

    def test_branch_names_issue(self):
        for b, n, want in (("272-calendar", 272, True), ("issue-19", 19, True), ("feature/19-x", 19, True),
                           ("sadr/issue-19-part", 19, True), ("fix-gh-19", 19, True),
                           ("factory/2026-10-19", 19, False), ("v19", 19, False), ("1272-x", 272, False)):
            self.assertEqual(issues.branch_names_issue(b, n), want, b)


class SignoffTest(unittest.TestCase):
    def test_pending_signoff_skips_the_issue(self):
        # Recorded from the #281 case: the owner asked a second maintainer, who has not replied.
        api = FakeApi({
            "repos/acme/app": {"has_issues": True}, "repos/acme/app/branches": [], "repos/acme/app/pulls": [],
            "repos/acme/app/issues?": [issue(281)],
            "repos/acme/app/issues/281/timeline": fixture_json("issues_signoff_timeline.json"),
            "repos/acme/app/collaborators/maint/permission": {"role_name": "admin"}})
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "graphql", lambda *a, **k: {}):
            ok, skipped, _ = issues.eligible("acme/app")
        self.assertEqual(ok, [])
        self.assertTrue(skipped[0]["pending_signoff"])
        self.assertEqual(skipped[0]["signoff"]["waiting_on"], ["partner"])
        self.assertIn("asked @partner for approval", skipped[0]["reason"])

    def test_reply_clears_signoff(self):
        writes = {"maint", "partner"}.__contains__
        ask = comment("maint", "@partner does this look good to you?", "2026-10-15T00:00:00Z")
        self.assertIsNotNone(issues.pending_signoff([ask], writes))
        self.assertIsNone(issues.pending_signoff([ask, comment("partner", "Yes.", "2026-10-16T00:00:00Z")], writes))
        # A mention without an ask, or an ask from someone without write access, is not a sign-off wait.
        self.assertIsNone(issues.pending_signoff([comment("maint", "cc @partner, FYI.", "x")], writes))
        self.assertIsNone(issues.pending_signoff([comment("rando", "@maint approve?", "x")], writes))


if __name__ == "__main__":
    unittest.main()
