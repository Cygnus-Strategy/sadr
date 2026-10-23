import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from helpers import fixture_json
import semgrep_api


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(routes, seen):
    """Route by path. A route value can be a callable that takes the query dict."""
    def urlopen(req, timeout=None):
        seen.append(req)
        path, _, query = req.full_url[len(semgrep_api.BASE):].partition("?")
        if path not in routes:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"no"))
        resp = routes[path]
        if callable(resp):
            resp = resp(urllib.parse.parse_qs(query))
        return FakeResponse(json.dumps(resp).encode())
    return urlopen


def by_type(sast=(), sca=(), status_ok=("open", "fixing", "reviewing")):
    """A findings route that answers per issue_type and status, and records the ref."""
    def route(q):
        if q.get("status", ["all"])[0] not in status_ok + ("all",):
            return {"findings": []}
        rows = {"sast": list(sast), "sca": list(sca)}[q["issue_type"][0]]
        return {"findings": [f for f in rows if f.get("status", "open") == q.get("status", [f.get("status", "open")])[0]]}
    return route


class SemgrepTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"SEMGREP_APP_TOKEN": "tok"}, clear=False)
        env.start()
        os.environ.pop("SEMGREP_DEPLOYMENT", None)
        self.addCleanup(env.stop)

    def test_token_only_from_env(self):
        with mock.patch.dict(os.environ, {"SEMGREP_APP_TOKEN": ""}):
            with self.assertRaises(semgrep_api.ApiError):
                semgrep_api.token()

    def test_https_guard(self):
        with mock.patch.object(semgrep_api, "BASE", "http://semgrep.dev/api/v1"):
            with self.assertRaises(ValueError):
                semgrep_api.build_url("/deployments")

    def test_pick_deployment(self):
        one = [{"id": 1, "slug": "acme"}]
        two = one + [{"id": 2, "slug": "other"}]
        self.assertEqual(semgrep_api.pick_deployment(None, one)["slug"], "acme")
        self.assertEqual(semgrep_api.pick_deployment("2", two)["slug"], "other")
        with mock.patch.dict(os.environ, {"SEMGREP_DEPLOYMENT": "other"}):
            self.assertEqual(semgrep_api.pick_deployment(None, two)["slug"], "other")
        for arg, deps in ((None, two), ("missing", two), (None, [])):
            with self.assertRaises(semgrep_api.ApiError):
                semgrep_api.pick_deployment(arg, deps)

    def routes(self, **extra):
        return {"/deployments": fixture_json("semgrep_deployments.json"),
                "/deployments/acme/projects": fixture_json("semgrep_projects.json"),
                "/deployments/acme/findings": by_type(fixture_json("semgrep_findings_sast.json")["findings"],
                                                      fixture_json("semgrep_findings_sca_fixing.json")["findings"]),
                "/deployments/115069/secrets": {"findings": []}, **extra}

    def test_findings_untriaged_and_auth_header(self):
        seen = []
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(), seen)), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            semgrep_api.main(["findings", "--repo", "acme/app", "--type", "sast", "--untriaged", "--json"])
        ids = [f["id"] for f in json.loads(out.getvalue())]
        self.assertEqual(ids, [1234, 1241])
        req = seen[-1]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertIn("repos=acme%2Fapp", req.full_url)
        self.assertIn("issue_type=sast", req.full_url)
        self.assertIn("ref=refs%2Fheads%2Fmain", req.full_url)

    def test_findings_default_all_types_and_active_statuses(self):
        # A recorded case: the certifi SCA finding is in status fixing, and the old default hid it.
        seen = []
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(), seen)), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            semgrep_api.main(["--json", "findings", "--repo", "acme/app"])
        rows = json.loads(out.getvalue())
        self.assertIn((1032518627, "sca", "fixing"), [(f["id"], f["type"], f["status"]) for f in rows])
        self.assertEqual({f["type"] for f in rows}, {"sast", "sca"})
        calls = [urllib.parse.parse_qs(r.full_url.partition("?")[2]) for r in seen if "/findings" in r.full_url]
        self.assertEqual({(c["issue_type"][0], c["status"][0]) for c in calls},
                         {(t, st) for t in ("sast", "sca") for st in ("open", "fixing", "reviewing")})
        self.assertTrue(any("/secrets" in r.full_url and "FINDING_STATUS_OPEN" in r.full_url for r in seen))

    def test_findings_status_all_and_choices(self):
        seen = []
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(), seen)), \
                mock.patch("sys.stdout", io.StringIO()):
            semgrep_api.main(["findings", "--repo", "acme/app", "--status", "all", "--json"])
        calls = [r.full_url for r in seen if "/findings" in r.full_url]
        self.assertTrue(calls and all("status=" not in u for u in calls))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            semgrep_api.main(["findings", "--status", "opne"])

    def test_findings_refs(self):
        rows = [{"id": 1, "ref": "refs/heads/main"}, {"id": 2, "ref": "refs/pull/80/merge"}]
        dep = {"id": 7, "slug": "acme"}
        with mock.patch.object(semgrep_api, "findings_one", return_value=rows), \
                mock.patch.object(semgrep_api, "projects", return_value=[{"name": "acme/x", "missing": True}]):
            # No project, so no primary branch: pull-request refs drop out.
            self.assertEqual([f["id"] for f in semgrep_api.findings(dep, ["acme/x"], "sast", "open")], [1])
            self.assertEqual([f["id"] for f in semgrep_api.findings(dep, ["acme/x"], "sast", "open",
                                                                     all_refs=True)], [1, 2])
            self.assertEqual([f["id"] for f in semgrep_api.findings(dep, ["acme/x"], "sast", "open",
                                                                     ref="refs/pull/80/merge")], [2])

    def test_ref_groups_follow_each_primary_branch(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(), [])):
            groups = semgrep_api.ref_groups({"id": 115069, "slug": "acme"}, [])
        self.assertEqual(groups, [("refs/heads/develop", ["acme/lib"]),
                                  ("refs/heads/main", ["acme/app", "acme/quiet"])])
        self.assertEqual(semgrep_api.full_ref("main"), "refs/heads/main")

    def test_secrets_use_numeric_id_and_cursor(self):
        pages = iter([{"findings": [{"id": 1}], "cursor": "c1"}, {"findings": [{"id": 2}]}])
        with mock.patch.object(semgrep_api, "call", side_effect=lambda m, p, params=None, body=None: next(pages)) as call:
            rows = semgrep_api.findings({"id": 7, "slug": "acme"}, ["acme/app"], "secrets", "open", all_refs=True)
        self.assertEqual([r["id"] for r in rows], [1, 2])
        self.assertEqual(call.call_args_list[0][0][1], "/deployments/7/secrets")
        self.assertEqual(call.call_args_list[1][0][2]["cursor"], "c1")
        self.assertEqual(call.call_args_list[0][0][2]["status"], "FINDING_STATUS_OPEN")
        self.assertEqual({r["type"] for r in rows}, {"secrets"})
        # Secrets have no fixing or reviewing status.
        self.assertEqual(semgrep_api.secrets({"id": 7}, [], "fixing"), [])

    def test_summary(self):
        rows = fixture_json("semgrep_findings_sast.json")["findings"]
        counts, total = semgrep_api.summarize({"sast": rows, "sca": [], "secrets": []}, ["acme/quiet"])
        self.assertEqual(total, 2)
        self.assertEqual(counts["acme/app"]["sast"], {"untriaged": 1, "ignored": 1})
        self.assertEqual(counts["acme/quiet"], {"sast": {}, "sca": {}, "secrets": {}})

    def test_summary_prints_one_row_per_repo(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(), [])), \
                mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()):
            semgrep_api.main(["summary"])
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines], ["acme/app", "acme/lib", "acme/quiet"])
        self.assertIn("sast: none; sca: none; secrets: none", lines[2])
        self.assertIn("sca: fixing 1", lines[0])
        self.assertIn("latest_scan=2026-10-21", lines[2])

    def test_projects_stale_scan(self):
        projs = fixture_json("semgrep_projects.json")["projects"]
        routes = self.routes(**{"/deployments/115069/scans/search": fixture_json("semgrep_scans.json")})
        dep = {"id": 115069, "slug": "acme"}
        with mock.patch("urllib.request.urlopen", fake_urlopen(routes, [])), \
                mock.patch.object(semgrep_api, "head_age_hours", return_value=30.0):
            fresh = semgrep_api.project_row(dep, projs[0], head="2604144cbe6e79f60548484ef5855baeaa5f33f2")
            old = semgrep_api.project_row(dep, projs[0], head="b" * 40)
            running = semgrep_api.project_row(dep, projs[0], head="a" * 40)
        self.assertEqual((fresh["touch_trigger"], old["touch_trigger"], running["touch_trigger"]), (False, True, False))
        self.assertIn("running", running["trigger_reason"])
        # The newest completed scan counts, not the running one.
        self.assertEqual((fresh["scan_commit"][:7], fresh["stale"]), ("2604144", False))
        self.assertTrue(old["stale"])
        self.assertIn("the head is bbbbbbb", old["reason"])
        with mock.patch("urllib.request.urlopen", fake_urlopen(self.routes(**{
                "/deployments/115069/scans/search": {"scans": []}}), [])), \
                mock.patch.object(semgrep_api, "default_head", return_value=("c" * 40, None)), \
                mock.patch.object(semgrep_api, "head_age_hours", return_value=None):
            none = semgrep_api.project_row(dep, projs[1])
        self.assertTrue(none["stale"])
        self.assertIn("no completed scan of refs/heads/develop", none["reason"])

    def test_trigger_decision(self):
        stale = {"stale": True}
        head = "h" * 40
        self.assertEqual(semgrep_api.trigger_decision(stale, [], head, 2.0, 6)[0], False)
        self.assertEqual(semgrep_api.trigger_decision(stale, [], head, 10.0, 6)[0], True)
        self.assertEqual(semgrep_api.trigger_decision(stale, [], head, None, 6)[0], None)
        failed = [{"commit": head, "status": "SCAN_STATUS_ERROR"}]
        self.assertIn("failed", semgrep_api.trigger_decision(stale, failed, head, 10.0, 6)[1])
        self.assertEqual(semgrep_api.trigger_decision({"stale": False}, [], head, 10.0, 6)[0], False)

    def test_projects_head_from_gh(self):
        from helpers import FakeApi
        import _gh
        api = FakeApi({"repos/acme/app/git/ref/heads/main": {"object": {"sha": "d" * 40}}})
        with mock.patch.object(_gh, "api", api):
            self.assertEqual(semgrep_api.default_head("acme/app", "refs/heads/main"), ("d" * 40, None))
            sha, why = semgrep_api.default_head("acme/app", "refs/heads/gone")
        self.assertIsNone(sha)
        self.assertIn("no refs/heads/gone", why)

    def test_lockfiles(self):
        routes = self.routes(**{"/deployments/115069/dependencies/repositories/6673126/lockfiles":
                                fixture_json("semgrep_lockfiles.json")})
        with mock.patch("urllib.request.urlopen", fake_urlopen(routes, [])), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            semgrep_api.main(["lockfiles", "--repo", "acme/app", "--repo", "acme/none", "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["lockfiles"][1], {"repo": "acme/app", "lockfile": "worker/uv.lock", "dependencies": 44})
        self.assertEqual(data["no_project"], ["acme/none"])

    def test_line_for_sca(self):
        f = {"type": "sca", **fixture_json("semgrep_findings_sca_fixing.json")["findings"][0]}
        line = semgrep_api.line_for(f)
        self.assertIn("GHSA-6q48-6c7w-9gw2", line)
        self.assertIn("certifi@2026.10.18 -> certifi 2026.10.19", line)
        self.assertIn("fixing/fixing", line)

    def test_triage_body(self):
        body = semgrep_api.triage_body("sast", "1234, 1240", "ignored", "false_positive", "x" * 4000)
        self.assertEqual(body["issue_ids"], [1234, 1240])
        self.assertEqual(len(body["new_note"]), 3000)
        self.assertEqual(body["new_triage_reason"], "false_positive")
        for args in (("sast", "1", None, None, None), ("sast", "1", "ignored", None, None),
                     ("sast", "1", "fixing", "duplicate", None), ("sast", "a,b", None, None, "n"),
                     ("sast", "", None, None, "n")):
            with self.assertRaises(ValueError):
                semgrep_api.triage_body(*args)

    def test_triage_posts_to_the_deployment(self):
        seen = []
        routes = {"/deployments": fixture_json("semgrep_deployments.json"), "/deployments/acme/triage": {"ok": True}}
        with mock.patch("urllib.request.urlopen", fake_urlopen(routes, seen)), mock.patch("sys.stdout", io.StringIO()):
            semgrep_api.main(["triage", "--type", "sast", "--ids", "1234", "--note", "fixing in PR 41"])
        self.assertEqual(seen[-1].get_method(), "POST")
        self.assertEqual(json.loads(seen[-1].data), {"issue_type": "sast", "issue_ids": [1234],
                                                     "new_note": "fixing in PR 41"})

    def test_http_error_exits_without_token(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen({}, [])), mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                semgrep_api.main(["deployments"])
        self.assertIn("HTTP 404", str(cm.exception.code))
        self.assertNotIn("tok", str(cm.exception.code).replace("token", ""))



class BaselineTest(unittest.TestCase):
    def setUp(self):
        from helpers import fixture
        self.log = fixture("semgrep_dryrun.log")

    def test_log_blob_has_every_finding(self):
        res = semgrep_api.parse_baseline(self.log)
        self.assertTrue(res["ok"], res["problems"])
        by = {r["rule"]: r for r in res["findings"]}
        self.assertEqual(len(res["findings"]), 5)
        self.assertEqual({t: c["total"] for t, c in res["counts"].items()}, {"sast": 3, "sca": 1, "secrets": 1})
        # Non-blocking (monitor mode) findings count, though the summary says 2.
        self.assertFalse(by["python.lang.security.audit.dynamic-urllib-use-detected."
                            "dynamic-urllib-use-detected"]["blocking"])
        sca = by["ssc-0f3a"]
        self.assertEqual((sca["package"], sca["version"], sca["reachable"], sca["lockfile"]),
                         ("follow-redirects", "1.15.5", False, "package-lock.json"))
        self.assertEqual(sca["advisories"], ["GHSA-cxjh-pqwp-8mfp", "CVE-2024-28849"])
        self.assertTrue(by["html.security.plaintext-http-link.plaintext-http-link"]["ignored"])
        self.assertEqual(by["generic.secrets.gitleaks.github-pat"]["type"], "secrets")

    def test_missing_blob_or_fatal_error_is_not_a_baseline(self):
        res = semgrep_api.parse_baseline("Scan Summary\nCI scan completed successfully.\n")
        self.assertFalse(res["ok"])
        self.assertIn("no findings blob", res["problems"][0])
        bad = self.log.replace('"errors": []', '"errors": [{"level": "error", "message": "Pro engine missing"}]')
        res = semgrep_api.parse_baseline(bad)
        self.assertFalse(res["ok"])
        self.assertIn("Pro engine missing", res["problems"][0])

    def test_json_output_is_joined(self):
        extra = {"results": [{"check_id": "go.lang.x", "path": "a.go", "start": {"line": 3}, "end": {"line": 3},
                              "extra": {"message": "m", "metadata": {}, "severity": "ERROR"}},
                             {"check_id": "c.lang.security.insecure-use-strcat-fn", "path": "main.c",
                              "start": {"line": 137}, "end": {"line": 137}, "extra": {"metadata": {}}}]}
        res = semgrep_api.parse_baseline(self.log, extra)
        self.assertEqual((len(res["findings"]), res["from_json_output_only"]), (6, 1))

    def test_match_api(self):
        res = semgrep_api.parse_baseline(self.log)
        api = [{"type": "sast", "id": 101, "status": "open", "triage_state": "ignored",
                "rule_name": "c.lang.security.insecure-use-strcat-fn", "location": {"file_path": "main.c", "line": 137}},
               {"type": "sca", "id": 202, "status": "fixing", "triage_state": "",
                "vulnerability_identifier": "GHSA-cxjh-pqwp-8mfp",
                "found_dependency": {"package": "follow-redirects", "version": "1.15.5"}}]
        rows = {r["rule"]: r for r in semgrep_api.match_api(res["findings"], api)}
        self.assertEqual((rows["c.lang.security.insecure-use-strcat-fn"]["api_id"],
                          rows["c.lang.security.insecure-use-strcat-fn"]["api_triage_state"]), (101, "ignored"))
        self.assertEqual((rows["ssc-0f3a"]["api_id"], rows["ssc-0f3a"]["api_status"]), (202, "fixing"))
        self.assertIsNone(rows["generic.secrets.gitleaks.github-pat"]["api_id"])

    def test_main_baseline_exit_codes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            good, bad = os.path.join(d, "good.log"), os.path.join(d, "bad.log")
            with open(good, "w") as f:
                f.write(self.log)
            with open(bad, "w") as f:
                f.write("nothing\n")
            with mock.patch("sys.stdout", io.StringIO()) as out:
                semgrep_api.main(["baseline", "--log", good, "--json"])
            self.assertEqual(json.loads(out.getvalue())["counts"]["sca"]["total"], 1)
            with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                semgrep_api.main(["baseline", "--log", bad])
            self.assertEqual(cm.exception.code, 2)

    def test_run_fails_loudly_without_semgrep(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(semgrep_api.ApiError) as cm:
                semgrep_api.run_dry_run(".", "/nonexistent/log")
        self.assertIn("not on PATH", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
