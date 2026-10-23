import json
import unittest
from unittest import mock

from helpers import NOW, FakeApi, fixture, fixture_json
import _gh
import repos


class SectionTest(unittest.TestCase):
    def test_opt_out_in_section_only(self):
        line = repos.opt_out(fixture("agents_optout.md"))
        self.assertTrue(line.startswith("Sadr: OFF."))

    def test_mentions_and_other_sections_do_not_opt_out(self):
        self.assertIsNone(repos.opt_out(fixture("agents_on.md")))
        self.assertIsNone(repos.opt_out("# AGENTS\n\nSadr: off\n"))
        self.assertIsNone(repos.opt_out(""))

    def test_section_ends_at_next_level_two_heading_but_not_in_fence(self):
        lines = repos.sadr_section(fixture("agents_optout.md"))
        self.assertIn("## Not a heading, inside a fence", lines)
        self.assertNotIn("- Sadr: off here is outside the section.", lines)

    def test_bold_opt_out(self):
        self.assertIsNotNone(repos.opt_out("## Sadr\n**Sadr: off** while we migrate.\n"))


class ClassifyTest(unittest.TestCase):
    def test_upkeep_and_feature_prs_skip_forks_and_others(self):
        upkeep, features = repos.classify(fixture_json("pulls_app.json"), "acme/app")
        self.assertEqual(upkeep["number"], 41)
        self.assertEqual([f["number"] for f in features], [43, 49])
        self.assertEqual(features[0]["issue"], 12)
        self.assertEqual((features[1]["issue"], features[1]["part"]), (272, "calendar"))


class OtherAutomationTest(unittest.TestCase):
    def test_bots_and_named_prefixes_list_every_file(self):
        api = FakeApi({
            "repos/acme/app/pulls/46/files": [{"filename": "uv.lock"}, {"filename": "src/app.py"}],
            "repos/acme/app/pulls/47/files": [{"filename": "web/package.json"}, {"filename": "web/package-lock.json"}],
            "repos/acme/app/pulls/48/files": [{"filename": "README.md"}],
        })
        with mock.patch.object(_gh, "api", api):
            rows = repos.other_automation(fixture_json("pulls_app.json"), "acme/app")
        # No hard-coded factory/ prefix: a human's factory/ branch is not automation by default.
        self.assertEqual([(r["number"], r["files"], r["manifest_files"]) for r in rows],
                         [(47, ["web/package.json", "web/package-lock.json"],
                           ["web/package.json", "web/package-lock.json"]),
                          (48, ["README.md"], [])])
        # Human PRs and Sadr's own PRs cost no file lookups.
        self.assertEqual(len(api.calls), 2)
        with mock.patch.object(_gh, "api", api):
            rows = repos.other_automation(fixture_json("pulls_app.json"), "acme/app", ["factory/"])
        self.assertEqual([(r["number"], r["files"], r["manifest_files"]) for r in rows][0],
                         (46, ["uv.lock", "src/app.py"], ["uv.lock"]))

    def test_manifest_patterns(self):
        for name in ("package.json", "a/b/uv.lock", "go.sum", ".github/workflows/ci.yml", "Dockerfile",
                     "docker/Dockerfile.prod", "compose.yaml", "tools/asc.py.lock", ".node-version", "action.yml"):
            self.assertTrue(repos.MANIFEST.search(name), name)
        for name in ("README.md", "src/package.py", "docs/workflows.md"):
            self.assertFalse(repos.MANIFEST.search(name), name)


class InferredRelatedTest(unittest.TestCase):
    FILES = {
        "Package.swift": 'let p = Package(dependencies: [.package(path: "../field-ui"), '
                         '.package(url: "https://github.com/apple/swift-log.git", from: "1.0.0")])',
        "App/App.xcodeproj/project.pbxproj": 'relativePath = ../field-ui;\n'
                                              'relativePath = "../../outside";\n'
                                              'path = ../shared-kit;',
        "web/package.json": '{"dependencies": {"ui": "file:../../design", "x": "github:acme/widgets#v1", '
                            '"inner": "file:../lib"}}',
        "go.mod": "module github.com/acme/app\nrequire github.com/acme/gokit v1.2.3\nreplace x => ../gotools\n",
        ".gitmodules": '[submodule "vendor/kit"]\n\turl = https://github.com/acme/kit.git\n'
                       '[submodule "third"]\n\turl = https://github.com/other/thing.git\n',
    }

    def test_sibling_paths_resolve_from_the_right_folder(self):
        self.assertEqual([r[0] for r in repos.sibling_refs("Package.swift", self.FILES["Package.swift"])],
                         ["field-ui"])
        pbx = repos.sibling_refs("App/App.xcodeproj/project.pbxproj", self.FILES["App/App.xcodeproj/project.pbxproj"])
        # The project folder is App/, so ../field-ui stays inside the repo and ../../outside leaves it.
        self.assertEqual([r[0] for r in pbx], ["outside"])
        self.assertEqual([r[0] for r in repos.sibling_refs("web/package.json", self.FILES["web/package.json"])],
                         ["design"])

    def test_inferred_related(self):
        exists = {"acme/field-ui", "acme/design", "acme/gotools", "acme/gokit", "acme/kit", "acme/widgets"}
        api = FakeApi({f"repos/{r}": {"full_name": r} for r in exists})
        with mock.patch.object(_gh, "api", api):
            found, unreachable = repos.inferred_related(
                "acme/app", "main", declared=["acme/kit"], files=list(self.FILES),
                fetch=self.FILES.get)
        by = {r["repo"]: r for r in found}
        self.assertEqual(sorted(by), ["acme/design", "acme/field-ui", "acme/gokit", "acme/gotools", "acme/widgets"])
        self.assertEqual((by["acme/field-ui"]["file"], by["acme/field-ui"]["kind"], by["acme/field-ui"]["line"]),
                         ("Package.swift", "path", 1))
        self.assertEqual(by["acme/widgets"]["kind"], "url")
        # The repo itself (module github.com/acme/app) and other owners never count.
        self.assertNotIn("acme/app", by)
        self.assertEqual([r["repo"] for r in unreachable], ["acme/outside"])

    def test_build_files_skip_vendored(self):
        tree = {"tree": [{"path": p, "type": "blob"} for p in
                         ("Package.swift", "node_modules/x/package.json", "a/b/go.mod", "README.md",
                          "App.xcodeproj/project.pbxproj", "vendor/y/go.mod")]}
        with mock.patch.object(_gh, "api", FakeApi({"repos/acme/app/git/trees/main": tree})):
            self.assertEqual(repos.build_files("acme/app", "main"),
                             ["Package.swift", "App.xcodeproj/project.pbxproj", "a/b/go.mod"])


class RelatedTest(unittest.TestCase):
    def test_slugs_skip_paths_and_other_sections(self):
        names = repos.slugs(repos.sadr_section(fixture("agents_related.md")))
        self.assertEqual(names, [("Cygnus-Strategy/field-ui", False), ("worker/src", False), ("acme/site", True),
                                 ("acme/gone", False), ("acme/app", False), ("acme/fenced-but-still-a-name", True)])

    def test_related_checks_that_each_repo_exists(self):
        api = FakeApi({"repos/Cygnus-Strategy/field-ui": {"full_name": "Cygnus-Strategy/field-ui"},
                       "repos/acme/site": {"full_name": "acme/site"},
                       "repos/acme/fenced-but-still-a-name": _gh.GhError("HTTP 403: Resource not accessible")})
        with mock.patch.object(_gh, "api", api):
            found, unreachable = repos.related(fixture("agents_related.md"), "acme/app")
        self.assertEqual(found, ["Cygnus-Strategy/field-ui", "acme/site"])
        self.assertEqual(unreachable, ["acme/gone", "acme/fenced-but-still-a-name"])
        self.assertNotIn("repos/worker/src", api.calls)
        # A name outside the scope that does not resolve is text, not a repo.
        with mock.patch.object(_gh, "api", FakeApi({})):
            found, unreachable = repos.related("## Sadr\n- Uses foo/bar style names.\n", "acme/app")
        self.assertEqual((found, unreachable), ([], []))

    def test_has_section(self):
        self.assertTrue(repos.has_section(fixture("agents_on.md")))
        self.assertFalse(repos.has_section("# A\n```\n## Sadr\n```\n"))
        self.assertFalse(repos.has_section(None))


class ClaimTest(unittest.TestCase):
    def test_only_recent_pushes_to_sadr_branches(self):
        claims = repos.claims_from_activity(fixture_json("activity_app.json"), NOW)
        self.assertEqual(claims, [{"branch": "sadr/upkeep", "pushed_at": "2026-10-22T11:40:00Z"}])

    def test_fallback_to_branch_commit_dates(self):
        api = FakeApi({
            "repos/acme/app/activity": _gh.GhError("HTTP 403"),
            "repos/acme/app/git/matching-refs/heads/sadr/": [
                {"ref": "refs/heads/sadr/upkeep", "object": {"sha": "a" * 40}},
                {"ref": "refs/heads/sadr/issue-3", "object": {"sha": "b" * 40}}],
            "repos/acme/app/commits/" + "a" * 40: {"commit": {"committer": {"date": "2026-10-22T11:30:00Z"}}},
            "repos/acme/app/commits/" + "b" * 40: {"commit": {"committer": {"date": "2026-10-20T11:30:00Z"}}},
        })
        with mock.patch.object(_gh, "api", api):
            self.assertEqual(repos.recent_claims("acme/app", NOW),
                             [{"branch": "sadr/upkeep", "pushed_at": "2026-10-22T11:30:00Z"}])


class DiscoverTest(unittest.TestCase):
    def test_discover_org(self):
        api = FakeApi({
            "repos/acme/app/contents/AGENTS.md?ref=main": fixture("agents_on.md"),
            "repos/acme/lib/contents/AGENTS.md?ref=trunk": fixture("agents_optout.md"),
            "repos/acme/app/pulls": fixture_json("pulls_app.json"),
            "repos/acme/lib/pulls": [],
            "repos/acme/app/activity": fixture_json("activity_app.json"),
            "repos/acme/lib/activity": [],
            "repos/acme/app/git/ref/heads/main": {"object": {"sha": "a" * 40}},
            "repos/acme/app/pulls/46/files": [{"filename": "uv.lock"}],
            # A server error on one repo becomes that repo's error.
            "repos/acme/lib/git/ref/heads/trunk": _gh.GhError("gh api: HTTP 503: Service Unavailable"),
        })
        run = mock.Mock(return_value=fixture("repo_list.json"))
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "run", run):
            rows, skipped = repos.discover(orgs=["acme"], at=NOW, workers=1)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0][:3], ["repo", "list", "acme"])
        self.assertEqual([r["repo"] for r in rows], ["acme/app", "acme/lib"])
        self.assertEqual({s["repo"]: s["reason"] for s in skipped},
                         {"acme/forked": "fork", "acme/old": "archived", "acme/blank": "empty"})
        app, lib = rows
        self.assertEqual(lib["default_branch"], "trunk")
        self.assertIn("HTTP 503", lib["error"])
        self.assertIsNone(lib["opted_out"])
        self.assertIsNone(app["error"])
        self.assertFalse(app["opted_out"])
        self.assertTrue(app["has_agents_md"])
        self.assertTrue(app["has_sadr_section"])
        self.assertEqual(app["default_head_sha"], "a" * 40)
        self.assertEqual(app["upkeep_pr"]["number"], 41)
        self.assertEqual([p["number"] for p in app["open_sadr_prs"]], [41, 43, 49])
        self.assertEqual([p["number"] for p in app["other_automation_prs"]], [47, 48])
        self.assertIsNone(app["facts_pr"])
        self.assertEqual(app["inferred_related_repos"], [])
        self.assertTrue(app["recently_claimed"])
        md = repos.render_md(rows, skipped)
        self.assertIn("2 repos in scope, 0 opted out, 3 skipped (forks, archived or empty), 1 with errors.", md)
        self.assertIn("| acme/lib | trunk | error: ", md)

    def test_empty_owner_listing_is_suspect(self):
        run = mock.Mock(return_value="[]")
        api = FakeApi({"users/acme": {"public_repos": 12}})
        with mock.patch.object(_gh, "api", api), mock.patch.object(_gh, "run", run), \
                mock.patch.object(_gh, "sleep", lambda s: None):
            with self.assertRaises(_gh.SuspectEmpty):
                repos.list_owner("acme")
        self.assertEqual(run.call_count, 2)
        with mock.patch.object(_gh, "api", FakeApi({"users/empty": {"public_repos": 0}})), \
                mock.patch.object(_gh, "run", run), mock.patch.object(_gh, "sleep", lambda s: None):
            self.assertEqual(repos.list_owner("empty"), [])

    def test_users_and_repos_are_deduplicated(self):
        info = json.loads(fixture("repo_list.json"))[0]
        calls = []

        def run(args):
            calls.append(args[:2])
            return json.dumps([info]) if args[1] == "list" else json.dumps(info)

        with mock.patch.object(_gh, "api", FakeApi({})), mock.patch.object(_gh, "run", run):
            rows, _ = repos.discover(users=["acme"], repos=["acme/app"], at=NOW, workers=1)
        self.assertEqual(calls, [["repo", "list"], ["repo", "view"]])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["has_agents_md"])

    def test_unknown_repo_becomes_an_error_row(self):
        def run(args):
            raise _gh.GhError("GraphQL: Could not resolve to a Repository with the name 'acme/nope'.")

        with mock.patch.object(_gh, "api", FakeApi({})), mock.patch.object(_gh, "run", run):
            rows, _ = repos.discover(repos=["acme/nope"], at=NOW, workers=1)
        self.assertEqual(rows[0]["repo"], "acme/nope")
        self.assertIn("Could not resolve", rows[0]["error"])

    def test_main_requires_a_scope(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                repos.main([])


if __name__ == "__main__":
    unittest.main()
