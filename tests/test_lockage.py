import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import HERE, NOW, FakeApi
import _gh
import lockage

LOCK = os.path.join(HERE, "fixtures", "lock")


def lock(*parts):
    return os.path.join(LOCK, *parts)


def names(rows, **kw):
    return [(r["name"], r["version"]) for r in rows if all(r.get(k) == v for k, v in kw.items())]


# Recorded registry answers, trimmed to the fields the script reads.
REGISTRY = {
    "https://registry.npmjs.org/wrangler": {"time": {"4.151.2": "2026-10-19T10:00:00.000Z"}},
    "https://registry.npmjs.org/@scope%2Fpkg": {"time": {"2.0.0": "2026-08-01T00:00:00.000Z",
                                                          "1.0.0": "2025-01-01T00:00:00.000Z"}},
    "https://proxy.golang.org/modernc.org/libc/@v/v1.79.0.info": {
        "Version": "v1.79.0", "Time": "2026-10-15T21:00:57Z",
        "Origin": {"VCS": "git", "URL": "https://gitlab.com/cznic/libc", "Ref": "refs/tags/v1.79.0"}},
    "https://proxy.golang.org/github.com/!burnt!sushi/toml/@v/v1.5.0.info": {"Time": "2025-03-04T00:00:00Z"},
    "https://pypi.org/pypi/fastapi/0.143.2/json": {"urls": [{"upload_time_iso_8601": "2026-10-10T12:00:00.000000Z"},
                                                            {"upload_time_iso_8601": "2026-10-10T11:59:00.000000Z"}]},
    "https://crates.io/api/v1/crates/serde/1.0.228": {"version": {"created_at": "2026-09-01T00:00:00Z"}},
    "https://jsr.io/@std/path/meta.json": {"versions": {"1.1.6": {"createdAt": "2026-06-30T10:24:29.944663Z"}}},
}


def fake_fetch(url):
    if url not in REGISTRY:
        raise LookupError(f"{url}: HTTP 404")
    return REGISTRY[url]


class ParseTest(unittest.TestCase):
    def test_npm(self):
        rows = lockage.parse(lock("npm", "package-lock.json"))
        self.assertEqual(names(rows), [("wrangler", "4.151.2"), ("@scope/pkg", "2.0.0"), ("@scope/pkg", "1.0.0"),
                                       ("local", "0.1.0"), ("gitdep", "1.0.0"), ("private", "3.0.0")])
        undated = {r["name"]: r["undated"] for r in rows if "undated" in r}
        self.assertIn("git", undated["gitdep"])
        self.assertIn("npm.acme.dev", undated["private"])

    def test_pnpm_strips_peer_suffix_and_skips_snapshots(self):
        rows = lockage.parse(lock("pnpm", "pnpm-lock.yaml"))
        self.assertEqual(names(rows), [("@babel/parser", "7.29.9"), ("react", "19.3.1"), ("wouter", "3.13.0")])

    def test_yarn_v1_and_berry(self):
        self.assertEqual(names(lockage.parse(lock("yarn1", "yarn.lock"))),
                         [("@babel/code-frame", "7.12.13"), ("lodash", "4.17.21")])
        rows = lockage.parse(lock("berry", "yarn.lock"))
        self.assertEqual(names(rows), [("app", "0.0.0-use.local"), ("lodash", "4.17.21"), ("left-pad", "1.3.0")])
        self.assertEqual(rows[0]["skip"], "local")
        self.assertIn("https", rows[2]["undated"])

    def test_bun_and_deno(self):
        rows = lockage.parse(lock("bun", "bun.lock"))
        self.assertEqual(names(rows), [("hono", "4.9.0"), ("@types/bun", "1.2.20"), ("lib", "workspace:packages/lib")])
        self.assertEqual(rows[2]["skip"], "local")
        rows = lockage.parse(lock("deno", "deno.lock"))
        self.assertEqual([(r["ecosystem"], r["name"], r["version"]) for r in rows], [
            ("npm", "@supabase/supabase-js", "2.118.0"), ("npm", "react-dom", "19.3.1"), ("jsr", "@std/path", "1.1.6"),
            ("url", "https://deno.land/std@0.224.0/path/mod.ts", "")])
        self.assertEqual(lockage.parse_bun("", binary=True)[0]["name"], "bun.lockb")

    def test_uv_and_script_lock(self):
        for name in ("uv.lock", "asc.py.lock"):
            rows = lockage.parse(lock("uv", name))
            self.assertEqual(names(rows), [("app", "0.1.0"), ("certifi", "2026.10.19"), ("fastapi", "0.143.2"),
                                           ("toolz", "1.0.0")])
            self.assertEqual(rows[0]["skip"], "local")
            # The earliest upload-time in the lockfile dates the entry without a network call.
            self.assertEqual(rows[1]["published_at"], "2026-10-20T02:24:57.000Z")
            self.assertIn("git", rows[3]["undated"])

    def test_poetry(self):
        rows = lockage.parse(lock("poetry", "poetry.lock"))
        self.assertEqual(names(rows), [("requests", "2.32.5"), ("mylib", "0.1.0"), ("forked", "1.0.0")])
        self.assertEqual(rows[1]["skip"], "local")
        self.assertIn("git source", rows[2]["undated"])

    def test_go_dates_built_modules_only(self):
        rows = lockage.parse(lock("go", "go.sum"))
        self.assertEqual(names(rows), [("github.com/BurntSushi/toml", "v1.5.0"), ("modernc.org/libc", "v1.79.0"),
                                       ("golang.org/x/sys", "v0.30.0")])
        self.assertIn("go.mod hash only", rows[2]["skip"])

    def test_cargo_and_swift(self):
        rows = lockage.parse(lock("cargo", "Cargo.lock"))
        self.assertEqual(names(rows), [("app", "0.1.0"), ("serde", "1.0.228"), ("patched", "0.2.0")])
        self.assertEqual((rows[0].get("skip"), "undated" in rows[2]), ("local", True))
        rows = lockage.parse(lock("swift", "Package.resolved"))
        self.assertEqual([(r["name"], r.get("repo"), "undated" in r) for r in rows], [
            ("swift-asn1", "apple/swift-asn1", False), ("swift-clocks", "pointfreeco/swift-clocks", False),
            ("edge", None, True), ("gitlab-pkg", None, True)])
        self.assertIn("branch main", rows[2]["undated"])

    def test_go_escape(self):
        self.assertEqual(lockage.go_escape("github.com/BurntSushi/toml"), "github.com/!burnt!sushi/toml")

    def test_find_lockfiles_skips_dependency_folders(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(LOCK, os.path.join(tmp, "repo"))
        os.makedirs(os.path.join(tmp, "repo", "npm", "node_modules", "x"))
        shutil.copy(lock("npm", "package-lock.json"), os.path.join(tmp, "repo", "npm", "node_modules", "x"))
        found = [os.path.relpath(f, tmp) for f in lockage.find_lockfiles([os.path.join(tmp, "repo")])]
        self.assertEqual(len(found), 13)
        self.assertFalse(any("node_modules" in f for f in found))


class DateTest(unittest.TestCase):
    def run_main(self, *args):
        with mock.patch.object(lockage, "http_json", fake_fetch), mock.patch.object(_gh, "now", lambda: NOW), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            code = lockage.main(list(args))
        return code, out.getvalue()

    def test_go_libc_is_young(self):
        # Recorded from acme/cli: the old factory took modernc.org/libc v1.79.0 inside the window.
        code, out = self.run_main(lock("go", "go.sum"))
        self.assertEqual(code, 1)
        self.assertIn("modernc.org/libc v1.79.0, published 2026-10-15T21:00Z, 6.6 days ago", out)
        self.assertIn("3 entries in 1 lockfiles: 1 younger than 7 days, 0 cannot be dated, 1 local or skipped.", out)

    def test_json_lists_young_and_undated(self):
        code, out = self.run_main("--json", lock("uv", "uv.lock"), lock("npm", "package-lock.json"))
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([(e["name"], e["version"]) for e in data["young"]],
                         [("certifi", "2026.10.19"), ("wrangler", "4.151.2")])
        self.assertEqual({e["name"] for e in data["undated"]}, {"toolz", "gitdep", "private"})
        self.assertEqual(data["counts"], {"young": 2, "ok": 3, "undated": 3, "skipped": 2})
        self.assertNotIn("entries", data)

    def test_cooldown_setting_and_all(self):
        code, out = self.run_main("--json", "--all", "--min-age-days", "1", lock("uv", "uv.lock"))
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["entries"]), 4)

    def test_lookup_failure_is_undated(self):
        rows = lockage.date_all(lockage.parse(lock("cargo", "Cargo.lock")) + [
            {"file": "x", "ecosystem": "npm", "name": "gone", "version": "1.0.0"}],
            dater=lockage.Dater(fake_fetch), at=NOW)
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["serde"]["date_source"], "crates.io")
        self.assertIn("HTTP 404", by["gone"]["undated"])

    def test_swift_uses_release_or_annotated_tag(self):
        api = FakeApi({
            "repos/apple/swift-asn1/git/ref/tags/1.7.3": {"object": {"type": "commit", "sha": "a" * 40}},
            "repos/apple/swift-asn1/releases/tags/1.7.3": {"published_at": "2026-05-01T00:00:00Z"},
            "repos/pointfreeco/swift-clocks/git/ref/tags/1.1.1": {"object": {"type": "commit", "sha": "b" * 40}},
        })
        rows = [r for r in lockage.parse(lock("swift", "Package.resolved")) if "repo" in r]
        with mock.patch.object(_gh, "api", api):
            rows = lockage.date_all(rows, dater=lockage.Dater(fake_fetch), at=NOW, workers=1)
        self.assertEqual(rows[0]["date_source"], "GitHub release")
        self.assertIn("cannot date", rows[1]["undated"])

    def test_npm_scoped_names_share_one_document(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return fake_fetch(url)
        rows = [r for r in lockage.parse(lock("npm", "package-lock.json")) if r["name"] == "@scope/pkg"]
        lockage.date_all(rows, dater=lockage.Dater(fetch), at=NOW, workers=1)
        self.assertEqual(calls, ["https://registry.npmjs.org/@scope%2Fpkg"])

    def test_not_a_lockfile(self):
        # A named file it cannot read is skipped and listed; with nothing else to read, that is an error.
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(lockage.main([os.path.join(HERE, "fixtures", "workflow.yml")]), 2)
        self.assertIn("skipped, not a lockfile", err.getvalue())
        with mock.patch("sys.stderr", io.StringIO()):
            code, out = self.run_main("--json", os.path.join(HERE, "fixtures", "workflow.yml"), lock("uv", "uv.lock"))
        self.assertEqual(json.loads(out)["unread_files"], [os.path.join(HERE, "fixtures", "workflow.yml")])
        self.assertIn(code, (0, 1))

    def test_exit_codes(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(lockage.main(["/nonexistent/uv.lock"]), 2)
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "package-lock.json")
            with open(bad, "w") as f:
                f.write("{not json")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(lockage.main([bad]), 2)
        code, _ = self.run_main(lock("go", "go.sum"))
        self.assertEqual(code, 1)
        code, _ = self.run_main("--min-age-days", "0", lock("go", "go.sum"))
        self.assertEqual(code, 0)

    def test_nix_flake_lock(self):
        code, out = self.run_main("--json", "--all", "--min-age-days", "1", lock("nix", "flake.lock"))
        data = json.loads(out)
        by = {e["name"]: e for e in data["entries"]}
        self.assertEqual(by["github:NixOS/nixpkgs"]["published_at"], "2026-10-20T00:00:00Z")
        self.assertIn("commit time", by["github:NixOS/nixpkgs"]["date_source"])
        self.assertEqual(by["./sub"]["status"], "skipped")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
