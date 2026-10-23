import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401
import _gh
import commit
import identity


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """A throwaway git repo with a base commit on main."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "config", "user.name", "Test")
        git(self.dir, "config", "user.email", "test@example.invalid")
        git(self.dir, "config", "commit.gpgsign", "false")
        self.write("README.md", "hello\n")
        self.write("old.txt", "bye\n")
        self.commit("base")
        self.base = git(self.dir, "rev-parse", "HEAD")
        git(self.dir, "switch", "-q", "-c", "sadr/upkeep")

    def write(self, path, text, mode=None):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        if mode:
            os.chmod(full, mode)

    def commit(self, msg):
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", msg)
        return git(self.dir, "rev-parse", "HEAD")

    def close(self):
        self.tmp.cleanup()


class FakeGitHub:
    """Records gh_input calls and answers like GitHub would."""

    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        self.n = 0

    def __call__(self, args, body):
        self.calls.append((args, body))
        if args == ["graphql"]:
            self.n += 1
            return {"data": {"createCommitOnBranch": {"commit": {"oid": f"{self.n:040x}",
                                                                 "signature": {"isValid": True}}}}}
        path = args[-1]
        if path.endswith("/git/blobs"):
            data = base64.b64decode(body["content"])
            sha = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True,
                                 cwd=self.repo.dir).stdout.decode().strip()
            return {"sha": sha}
        if path.endswith("/git/trees"):
            # Build the tree locally with the same entries.
            idx = os.path.join(self.repo.dir, ".git", "fake-index")
            env = {**os.environ, "GIT_INDEX_FILE": idx}
            subprocess.run(["git", "read-tree", body["base_tree"]], cwd=self.repo.dir, env=env, check=True)
            for e in body["tree"]:
                if e["sha"] is None:
                    subprocess.run(["git", "update-index", "--force-remove", e["path"]], cwd=self.repo.dir,
                                   env=env, check=True)
                else:
                    subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                                    f"{e['mode']},{e['sha']},{e['path']}"], cwd=self.repo.dir, env=env, check=True)
            sha = subprocess.run(["git", "write-tree"], cwd=self.repo.dir, env=env, capture_output=True,
                                 text=True).stdout.strip()
            os.unlink(idx)
            return {"sha": sha}
        if path.endswith("/git/commits"):
            self.n += 1
            return {"sha": f"{self.n:040x}", "verification": {"verified": True}}
        return {}


class CommitTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def run_publish(self, tip=None, verified=True, **kw):
        fake = FakeGitHub(self.r)
        with mock.patch.object(commit, "remote_tip", return_value=tip), \
                mock.patch.object(commit, "default_tip", return_value=self.r.base), \
                mock.patch.object(commit, "read_verified", return_value=verified), \
                mock.patch.object(commit, "gh_input", fake):
            res = commit.publish("acme/app", "sadr/upkeep", self.r.dir, sync=False, **kw)
        return res, fake

    def test_graphql_new_branch(self):
        self.r.write("README.md", "hello world\n")
        os.unlink(os.path.join(self.r.dir, "old.txt"))
        self.r.write("src/new.py", "print(1)\n")
        self.r.commit("sadr: update readme\n\nLonger body.")
        res, fake = self.run_publish(method="graphql")
        self.assertEqual((res["method"], res["created_ref"], res["base"]), ("graphql", True, self.r.base))
        (ref_args, ref_body), (gq_args, gq_body) = fake.calls
        self.assertEqual(ref_body, {"ref": "refs/heads/sadr/upkeep", "sha": self.r.base})
        inp = gq_body["variables"]["input"]
        self.assertEqual(inp["expectedHeadOid"], self.r.base)
        self.assertEqual(inp["message"], {"headline": "sadr: update readme", "body": "Longer body."})
        self.assertEqual(inp["fileChanges"]["deletions"], [{"path": "old.txt"}])
        adds = {a["path"]: base64.b64decode(a["contents"]).decode() for a in inp["fileChanges"]["additions"]}
        self.assertEqual(adds, {"README.md": "hello world\n", "src/new.py": "print(1)\n"})
        self.assertEqual(res["commits"][0]["verified"], True)

    def test_chained_expected_head_on_existing_branch(self):
        self.r.write("a.txt", "1\n")
        first = self.r.commit("one")
        self.r.write("b.txt", "2\n")
        self.r.commit("two")
        res, fake = self.run_publish(tip=first, method="graphql")
        self.assertEqual(len(res["commits"]), 1)
        self.assertEqual(fake.calls[0][1]["variables"]["input"]["expectedHeadOid"], first)
        self.assertFalse(res["created_ref"])

    def test_gitdata_is_default_with_sadr_author_and_no_committer(self):
        self.r.write("a.txt", "1\n")
        local = self.r.commit("sadr: one")
        res, fake = self.run_publish()
        self.assertEqual((res["method"], res["created_ref"]), ("gitdata", True))
        body = [b for a, b in fake.calls if a[-1].endswith("/git/commits")][0]
        self.assertNotIn("committer", body)
        self.assertEqual(body["author"], {"name": "Sadr", "email": "test@example.invalid",
                                          "date": git(self.r.dir, "log", "-1", "--format=%aI", local)})
        self.assertEqual((res["commits"][0]["verified"], res["unverified"]), (True, []))
        res, fake = self.run_publish(author={"name": "Sadr", "email": "1+s[bot]@users.noreply.github.com"})
        body = [b for a, b in fake.calls if a[-1].endswith("/git/commits")][0]
        self.assertEqual(body["author"]["email"], "1+s[bot]@users.noreply.github.com")

    def test_unverified_commit_warns_and_exits_zero(self):
        self.r.write("a.txt", "1\n")
        self.r.commit("sadr: one")
        with mock.patch.object(_gh, "api", return_value={"verification": {"verified": False}}) as api:
            self.assertIs(commit.read_verified("acme/app", "a" * 40), False)
        api.assert_called_once_with(f"repos/acme/app/git/commits/{'a' * 40}")
        fake = FakeGitHub(self.r)
        with mock.patch.object(commit, "remote_tip", return_value=None), \
                mock.patch.object(commit, "default_tip", return_value=self.r.base), \
                mock.patch.object(commit, "read_verified", return_value=False), \
                mock.patch.object(commit, "gh_input", fake), \
                mock.patch.dict(os.environ, {"SADR_APP_SLUG": ""}), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            code = commit.main(["--repo", "acme/app", "--branch", "sadr/upkeep", "--dir", self.r.dir, "--no-sync"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: GitHub did not verify 1 commits", err.getvalue())
        self.assertIn("--method graphql", err.getvalue())

    def test_executable_and_symlink_need_gitdata(self):
        self.r.write("run.sh", "#!/bin/sh\n", mode=0o755)
        os.symlink("README.md", os.path.join(self.r.dir, "link"))
        os.unlink(os.path.join(self.r.dir, "old.txt"))
        local = self.r.commit("tools")
        why = commit.graphql_problems(commit.changes(self.r.dir, self.r.base, local))
        self.assertEqual(sorted(why), ["link: a symlink", "run.sh: an executable file (mode 100755)"])
        with mock.patch.object(commit, "remote_tip", return_value=None), \
                mock.patch.object(commit, "default_tip", return_value=self.r.base):
            with self.assertRaises(commit.Unexpressible) as cm:
                commit.publish("acme/app", "sadr/upkeep", self.r.dir, method="graphql", sync=False)
        self.assertIn("Use --method gitdata", str(cm.exception))
        res, fake = self.run_publish()
        self.assertEqual(res["method"], "gitdata")
        trees = [b for a, b in fake.calls if a[-1].endswith("/git/trees")]
        modes = {e["path"]: e["mode"] for e in trees[0]["tree"]}
        self.assertEqual(modes, {"run.sh": "100755", "link": "120000", "old.txt": "100644"})
        # The ref is created last, at the new commit.
        self.assertEqual(fake.calls[-1][1]["ref"], "refs/heads/sadr/upkeep")

    def test_parent_missing_on_github_stops(self):
        self.r.write("a.txt", "1\n")
        mine = self.r.commit("mine")
        git(self.r.dir, "switch", "-q", "main")
        self.r.write("main.txt", "m\n")
        gone = self.r.commit("on a remote branch that GitHub no longer has")
        git(self.r.dir, "update-ref", "refs/remotes/origin/gone", gone)
        git(self.r.dir, "switch", "-q", "sadr/upkeep")
        git(self.r.dir, "merge", "-q", "--no-edit", "main")
        with mock.patch.object(_gh, "api", return_value=None):
            res = None
            with self.assertRaises(commit.PublishError):
                res, _ = self.run_publish(tip=mine)
        self.assertIsNone(res)

    def test_merge_commit_uses_gitdata_with_two_parents(self):
        self.r.write("a.txt", "1\n")
        mine = self.r.commit("mine")
        git(self.r.dir, "switch", "-q", "main")
        self.r.write("main.txt", "m\n")
        main2 = self.r.commit("main moves")
        git(self.r.dir, "update-ref", "refs/remotes/origin/main", main2)
        git(self.r.dir, "switch", "-q", "sadr/upkeep")
        git(self.r.dir, "merge", "-q", "--no-edit", "origin/main")
        with mock.patch.object(_gh, "api", return_value={"sha": main2}) as api:
            res, fake = self.run_publish(tip=mine)
        api.assert_called_once_with(f"repos/acme/app/git/commits/{main2}")
        # main2 is on GitHub already: only the merge commit is recreated.
        self.assertEqual(len(res["commits"]), 1)
        self.assertEqual(res["method"], "gitdata")
        commits = [b for a, b in fake.calls if a[-1].endswith("/git/commits")]
        self.assertEqual(commits[-1]["parents"], [mine, main2])
        self.assertEqual(fake.calls[-1][0][:3], ["-X", "PATCH", "repos/acme/app/git/refs/heads/sadr/upkeep"])
        self.assertEqual(fake.calls[-1][1]["force"], False)

    def test_moved_remote_branch_publishes_nothing(self):
        self.r.write("a.txt", "1\n")
        self.r.commit("one")
        with mock.patch.object(commit, "remote_tip", return_value="f" * 40), \
                mock.patch.object(commit, "gh_input") as gi:
            with self.assertRaises(commit.Moved):
                commit.publish("acme/app", "sadr/upkeep", self.r.dir, sync=False)
        gi.assert_not_called()

    def test_dry_run_writes_nothing(self):
        self.r.write("a.txt", "1\n")
        self.r.commit("one")
        res, fake = self.run_publish(dry_run=True)
        self.assertEqual((fake.calls, len(res["commits"]), res["dry_run"]), ([], 1, True))

    def test_nothing_to_publish(self):
        res, fake = self.run_publish(tip=self.r.base)
        self.assertEqual((res["commits"], fake.calls), ([], []))

    def test_main_refuses_other_branches(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            commit.main(["--repo", "acme/app", "--branch", "main"])

    def test_sync_moves_local_branch(self):
        self.r.write("a.txt", "1\n")
        self.r.commit("one")
        tree = git(self.r.dir, "rev-parse", "HEAD^{tree}")
        # Pretend GitHub made a commit with the same tree.
        remote = git(self.r.dir, "commit-tree", tree, "-p", self.r.base, "-m", "one")
        real = commit.git

        def fake_git(args, cwd, binary=False):
            return "" if args[0] == "fetch" else real(args, cwd, binary)
        with mock.patch.object(commit, "git", fake_git):
            commit.sync_local(self.r.dir, "sadr/upkeep", remote)
        self.assertEqual(git(self.r.dir, "rev-parse", "HEAD"), remote)
        self.assertEqual(git(self.r.dir, "status", "--porcelain"), "")


class IdentityTest(unittest.TestCase):
    def test_bot_identity(self):
        with mock.patch.object(_gh, "api", return_value={"id": 123456, "login": "sadr-acme[bot]"}) as api:
            ident = identity.bot_identity("sadr-acme")
        api.assert_called_once_with("/users/sadr-acme%5Bbot%5D")
        self.assertEqual((ident["name"], ident["email"]),
                         ("Sadr", "123456+sadr-acme[bot]@users.noreply.github.com"))

    def test_unknown_slug(self):
        with mock.patch.object(_gh, "api", return_value=None):
            with self.assertRaises(LookupError):
                identity.bot_identity("nope")
        with self.assertRaises(ValueError):
            identity.bot_identity("bad slug/../x")

    def test_formats_and_placeholder(self):
        ident = {"name": "Sadr", "email": "1+s[bot]@users.noreply.github.com"}
        self.assertEqual(identity.render(ident, "git"),
                         "-c user.name=Sadr -c 'user.email=1+s[bot]@users.noreply.github.com'")
        self.assertIn("GIT_COMMITTER_EMAIL='1+s[bot]@users.noreply.github.com'", identity.render(ident, "env"))
        with mock.patch("sys.stdout", io.StringIO()) as out, mock.patch.object(_gh, "api") as api:
            self.assertEqual(identity.main(["--placeholder", "--json"]), 0)
        api.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["email"], "sadr-preview@localhost.invalid")


if __name__ == "__main__":
    unittest.main()
