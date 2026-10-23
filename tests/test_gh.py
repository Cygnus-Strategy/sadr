import json
import subprocess
import unittest
from unittest import mock

from helpers import FakeApi
import _gh


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess(["gh"], code, stdout=stdout, stderr=stderr)


class GhTest(unittest.TestCase):
    def test_api_paginate_flattens_pages(self):
        pages = json.dumps([[{"n": 1}, {"n": 2}], [{"n": 3}]])
        with mock.patch("subprocess.run", return_value=done(pages)) as run:
            self.assertEqual(_gh.api("repos/a/b/pulls", paginate=True), [{"n": 1}, {"n": 2}, {"n": 3}])
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["gh", "api"])
        self.assertIn("--paginate", args)
        self.assertIn("--slurp", args)

    def test_api_404_returns_none(self):
        with mock.patch("subprocess.run", return_value=done(code=1, stderr="gh: Not Found (HTTP 404)")):
            self.assertIsNone(_gh.api("repos/a/b/contents/AGENTS.md", raw=True))

    def test_api_other_error_raises(self):
        with mock.patch("subprocess.run", return_value=done(code=1, stderr="HTTP 403: rate limit")):
            with self.assertRaises(_gh.GhError):
                _gh.api("repos/a/b")

    def test_raw_sets_accept_header(self):
        with mock.patch("subprocess.run", return_value=done("# AGENTS")) as run:
            self.assertEqual(_gh.api("repos/a/b/contents/AGENTS.md", raw=True), "# AGENTS")
        self.assertIn("Accept: application/vnd.github.raw", run.call_args[0][0])

    def test_graphql_types_variables_and_raises_errors(self):
        with mock.patch("subprocess.run", return_value=done(json.dumps({"data": {"x": 1}}))) as run:
            self.assertEqual(_gh.graphql("query", owner="a", number=5, after=None), {"x": 1})
        args = run.call_args[0][0]
        self.assertIn("owner=a", args)
        self.assertEqual(args[args.index("number=5") - 1], "-F")
        self.assertNotIn("after=None", " ".join(args))
        with mock.patch("subprocess.run", return_value=done(json.dumps({"errors": [{"message": "bad"}]}))):
            with self.assertRaises(_gh.GhError):
                _gh.graphql("query")

    def test_run_retries_server_errors(self):
        results = [done(code=1, stderr="gh: HTTP 503: Service Unavailable"),
                   done(code=1, stderr="read: connection reset by peer"), done("ok")]
        waits = []
        with mock.patch("subprocess.run", side_effect=results), mock.patch.object(_gh, "sleep", waits.append):
            self.assertEqual(_gh.run(["api", "x"]), "ok")
        self.assertEqual(waits, [2, 4])

    def test_run_gives_up_after_retries_and_does_not_retry_client_errors(self):
        with mock.patch("subprocess.run", return_value=done(code=1, stderr="HTTP 502")) as run, \
                mock.patch.object(_gh, "sleep", lambda s: None):
            with self.assertRaises(_gh.GhError):
                _gh.run(["api", "x"])
        self.assertEqual(run.call_count, _gh.RETRIES + 1)
        with mock.patch("subprocess.run", return_value=done(code=1, stderr="HTTP 422: bad")) as run:
            with self.assertRaises(_gh.GhError) as cm:
                _gh.run(["api", "x"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(cm.exception.status, 422)

    def test_empty_page_is_confirmed_without_pagination(self):
        # The flaky answer from the preview run: one empty page, then the real list.
        results = [done("[[]]"), done(json.dumps([{"n": 1}])), done(json.dumps([[{"n": 1}]]))]
        with mock.patch("subprocess.run", side_effect=results) as run, mock.patch.object(_gh, "sleep", lambda s: None):
            self.assertEqual(_gh.api("repos/a/b/issues", paginate=True), [{"n": 1}])
        self.assertNotIn("--paginate", run.call_args_list[1][0][0])

    def test_really_empty_list_is_fine(self):
        with mock.patch("subprocess.run", side_effect=[done("[[]]"), done("[]")]):
            self.assertEqual(_gh.api("repos/a/b/issues", paginate=True), [])

    def test_suspect_empty_raises(self):
        def answer(args, **kw):
            return done("[[]]") if "--paginate" in args else done(json.dumps([{"n": 1}]))
        with mock.patch("subprocess.run", side_effect=answer), mock.patch.object(_gh, "sleep", lambda s: None):
            with self.assertRaises(_gh.SuspectEmpty):
                _gh.api("repos/a/b/issues", paginate=True)

    def test_tag_date_release_then_tagger_never_commit(self):
        routes = {
            "repos/a/b/git/ref/tags/v1.0.0": {"object": {"type": "commit", "sha": "c" * 40}},
            "repos/a/b/releases/tags/v1.0.0": {"published_at": "2026-09-01T00:00:00Z"},
            "repos/a/b/git/ref/tags/v1.1.0": {"object": {"type": "tag", "sha": "t" * 40}},
            "repos/a/b/git/tags/" + "t" * 40: {"tagger": {"date": "2026-09-02T00:00:00Z"}, "object": {"sha": "d" * 40}},
            "repos/a/b/git/ref/tags/v1.2.0": {"object": {"type": "commit", "sha": "e" * 40}},
        }
        with mock.patch.object(_gh, "api", FakeApi(routes)):
            self.assertEqual(_gh.tag_date("a/b", "v1.0.0"), ("c" * 40, "2026-09-01T00:00:00Z", "release"))
            self.assertEqual(_gh.tag_date("a/b", "v1.1.0"), ("d" * 40, "2026-09-02T00:00:00Z", "annotated tag"))
            sha, when, why = _gh.tag_date("a/b", "v1.2.0")
            self.assertEqual((sha, when), ("e" * 40, None))
            self.assertIn("cannot date", why)
            self.assertEqual(_gh.tag_date("a/b", "v9.0.0")[:2], (None, None))

    def test_parse_time_semgrep_style(self):
        t = _gh.parse_time("2026-10-22 17:57:12.972886")
        self.assertEqual((t.hour, t.tzinfo is not None), (17, True))
        self.assertEqual(_gh.parse_time("2026-10-14T03:30:59.842900925Z").second, 59)

    def test_json_before_or_after_subcommand(self):
        import argparse
        ap = argparse.ArgumentParser()
        _gh.add_json(ap)
        sub = ap.add_subparsers(dest="cmd")
        _gh.add_json(sub.add_parser("x"), sub=True)
        self.assertTrue(ap.parse_args(["--json", "x"]).json)
        self.assertTrue(ap.parse_args(["x", "--json"]).json)
        self.assertFalse(ap.parse_args(["x"]).json)

    def test_semver(self):
        self.assertEqual(_gh.semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(_gh.semver("10.0.1"), (10, 0, 1))
        for bad in ("v1", "v1.2", "v1.2.3-rc.1", "release-1.2.3", "", None):
            self.assertIsNone(_gh.semver(bad))


if __name__ == "__main__":
    unittest.main()
