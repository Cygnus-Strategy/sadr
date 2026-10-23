import io
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import HERE, NOW, FakeApi, fixture, fixture_json
import _gh
import _registry
import pins

WORKFLOW = os.path.join(HERE, "fixtures", "workflow.yml")
TOOLS = os.path.join(HERE, "fixtures", "workflow_tools.yml")
DOCKERFILE = os.path.join(HERE, "fixtures", "Dockerfile.app")
COMPOSE = os.path.join(HERE, "fixtures", "compose.yaml")
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
UV_DIGEST = "sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336"
PY_DIGEST = "sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619"


def commit(sha, date="2026-07-01T00:00:00Z"):
    return {"sha": sha, "commit": {"committer": {"date": date}}}


def tag(repo, name, sha, published=None, tagger=None):
    """Routes for one tag: a lightweight or annotated ref, and maybe a release."""
    routes = {}
    if tagger:
        routes[f"repos/{repo}/git/ref/tags/{name}"] = {"object": {"type": "tag", "sha": "t" * 40}}
        routes[f"repos/{repo}/git/tags/{'t' * 40}"] = {"tagger": {"date": tagger}, "object": {"sha": sha}}
    else:
        routes[f"repos/{repo}/git/ref/tags/{name}"] = {"object": {"type": "commit", "sha": sha}}
    if published:
        routes[f"repos/{repo}/releases/tags/{name}"] = {"published_at": published}
    return routes


def tag_routes():
    r = {}
    r.update(tag("actions/checkout", "v7.0.1", CHECKOUT_SHA, "2026-07-17T18:45:11Z"))
    # The cache tag points to another commit than the pin.
    r.update(tag("actions/cache", "v5.0.0", "9" * 40, "2026-06-01T00:00:00Z"))
    r.update(tag("github/codeql-action", "v4.1.0", "3" * 40, "2026-09-01T00:00:00Z"))
    # Released two days ago.
    r.update(tag("actions/fresh", "v1.2.0", "4" * 40, "2026-10-20T12:00:00Z"))
    r.update(tag("actions/moving", "v3", "5" * 40))
    # No release, but an annotated tag: the tagger date counts, and it is old enough.
    r.update(tag("acme/shared", "v2.0.0", "6" * 40, tagger="2026-09-01T00:00:00Z"))
    r.update(tag("actions/setup-node", "v7.1.0", "949feb2413d6458794dcd2491c4babbbce0c15c1", "2026-09-01T00:00:00Z"))
    r.update(tag("actions/setup-python", "v6.0.0", "a26af69be951a213d495a4c3e4e4022e16d87065", "2026-09-01T00:00:00Z"))
    r.update(tag("astral-sh/setup-uv", "v7.1.2", "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86", "2026-09-01T00:00:00Z"))
    r.update(tag("actions/setup-go", "v6.0.0", "44694675825211faa026b3c33043df3e48a5fa00", "2026-09-01T00:00:00Z"))
    return r


class FakeRegistry:
    """Stand-in for _registry.Client: digest -> created time."""

    def __init__(self, created):
        self.created_at = created
        self.calls = []

    def created(self, registry, repo, ref):
        self.calls.append((registry, repo, ref))
        if ref not in self.created_at:
            raise _registry.RegistryError(f"{registry}/{repo} manifests/{ref}: HTTP 404")
        return ref, self.created_at[ref]


def registry():
    return FakeRegistry({UV_DIGEST: "2026-10-14T03:30:59.842900925Z", PY_DIGEST: "2026-10-20T00:00:00Z",
                         "sha256:" + "1" * 64: "2026-09-01T00:00:00Z"})


class ParseTest(unittest.TestCase):
    def test_parse_uses(self):
        rows = pins.parse_uses("w.yml", fixture("workflow.yml"))
        self.assertEqual(len(rows), 10)
        first = rows[0]
        self.assertEqual((first["action"], first["ref"], first["version"], first["line"]),
                         ("actions/checkout", CHECKOUT_SHA, "v7.0.1", 7))
        self.assertEqual(rows[2]["action"], "actions/cache")  # quoted
        self.assertEqual(rows[4]["action"], "github/codeql-action")  # sub-path
        self.assertEqual(rows[7]["skip"], "local action")
        self.assertEqual((rows[8]["kind"], rows[8]["uses"]), ("image", "alpine:3.22"))  # docker://
        self.assertEqual(rows[9]["action"], "acme/shared")  # reusable workflow
        self.assertEqual(rows[9]["version"], "v2.0.0")

    def test_comment_with_extra_text(self):
        row = pins.parse_uses("w.yml", "  - uses: a/b@" + "c" * 40 + " # v1.0.4, 7+ days old\n")[0]
        self.assertEqual(row["version"], "v1.0.4")

    def test_dockerfile_images_skip_stages_args_and_scratch(self):
        rows = pins.parse_file("Dockerfile", fixture("Dockerfile.app"))
        images = [(r["line"], r["uses"], r.get("skip")) for r in rows if r["kind"] == "image"]
        self.assertEqual([i[:2] for i in images], [
            (3, "python:3.15-slim"),
            (4, "ghcr.io/astral-sh/uv:0.13.1@" + UV_DIGEST),
            (7, "${BASE}"),
            (9, "python:3.15-slim@" + PY_DIGEST),
            (10, "scratch")])
        self.assertIn("build argument", images[2][2])
        self.assertIn("scratch", images[4][2])
        # "FROM build" and "COPY --from=build" name a stage, not an image.
        self.assertNotIn(6, [i[0] for i in images])
        self.assertNotIn(8, [i[0] for i in images])

    def test_compose_images(self):
        rows = pins.parse_file("compose.yaml", fixture("compose.yaml"))
        self.assertEqual([(r["uses"], "skip" in r) for r in rows], [
            ("ghcr.io/acme/app:1.2.3@sha256:" + "1" * 64, False), ("postgres:18", False),
            ("${CACHE_IMAGE:-redis:8}", True)])

    def test_setup_inputs(self):
        rows = [r for r in pins.parse_file("w.yml", fixture("workflow_tools.yml")) if r["kind"] == "setup"]
        got = {(r["line"], r["input"]): (r.get("problems"), r.get("skip")) for r in rows}
        self.assertEqual(got[(13, "node-version")][0], ["node-version '24' is not an exact version (X.Y.Z)"])
        self.assertEqual(got[(18, "python-version")][0], [])
        self.assertEqual(got[(19, "check-latest")][0], ["check-latest: true installs the newest release"])
        self.assertEqual(got[(22, "version")][1], "set by an expression")
        self.assertIn("1.25.x", got[(26, "go-version")][0][0])
        self.assertEqual(got[(40, "node-version")][0], [])
        # `cache: npm` and `python-version-file` are not version inputs.
        self.assertEqual(len(rows), 6)

    def test_installers(self):
        rows = [r for r in pins.parse_file("w.yml", fixture("workflow_tools.yml")) if r["kind"] == "installer"]
        got = [(r["tool"], r["package"], r["version"], bool(r.get("problems")), "skip" in r) for r in rows]
        self.assertEqual(got, [
            ("npx", "wrangler", "4.151.0", False, False),
            ("npx", "eslint", "latest", True, False),
            ("uvx", "ruff", None, True, False),
            ("uvx", "semgrep", "1.181.0", False, False),
            ("pipx run", "black", "24", True, False),
            ("npx", "tsc", None, False, True)])
        docker = [r for r in pins.parse_file("Dockerfile", fixture("Dockerfile.app")) if r["kind"] == "installer"]
        self.assertEqual([(r["package"], r["version"]) for r in docker], [("wrangler", "4")])

    def test_scoped_npm_package(self):
        self.assertEqual(pins.split_spec("npx", "@biomejs/biome@2.1.0"), ("@biomejs/biome", "2.1.0"))
        self.assertEqual(pins.split_spec("npx", "@biomejs/biome"), ("@biomejs/biome", None))


class CheckTest(unittest.TestCase):
    def test_check_reports_each_problem(self):
        with mock.patch.object(_gh, "api", FakeApi(tag_routes())):
            rows = pins.check([WORKFLOW], 7, resolver=pins.Resolver(registry()), at=NOW)
        by_line = {r["line"]: r.get("problems") for r in rows}
        self.assertEqual(by_line[7], [])
        self.assertIn("not pinned to a 40-character commit SHA (@v7)", by_line[8][0])
        self.assertIn("is not actions/cache@v5.0.0", by_line[9][0])
        self.assertEqual(by_line[10], ["no '# vX.Y.Z' version comment"])
        self.assertEqual(by_line[11], [])
        self.assertIn("younger than 7 days", by_line[12][0])
        self.assertEqual(by_line[13][0], "version comment v3 is not an exact vX.Y.Z")
        self.assertIn("cannot date", by_line[13][1])
        self.assertIsNone(by_line[14])
        self.assertIn("not pinned to a digest", by_line[15][0])
        self.assertEqual(by_line[17], [])

    def test_check_images_and_tools(self):
        reg = registry()
        with mock.patch.object(_gh, "api", FakeApi(tag_routes())):
            rows = pins.check([DOCKERFILE, COMPOSE, TOOLS], 7, resolver=pins.Resolver(reg), at=NOW)
        bad = {(os.path.basename(r["file"]), r["line"]): r["problems"] for r in rows if r.get("problems")}
        self.assertIn("not pinned to a digest", bad[("Dockerfile.app", 3)][0])
        self.assertNotIn(("Dockerfile.app", 4), bad)  # uv digest built 8 days ago
        self.assertIn("younger than 7 days", bad[("Dockerfile.app", 9)][0])
        self.assertIn("wrangler@4", bad[("Dockerfile.app", 5)][0])
        self.assertNotIn(("compose.yaml", 3), bad)
        self.assertIn("not pinned to a digest", bad[("compose.yaml", 5)][0])
        self.assertIn("not pinned to a digest", bad[("workflow_tools.yml", 6)][0])
        self.assertIn("koreader/kosync:latest", bad[("workflow_tools.yml", 9)][0])
        self.assertIn("not pinned to a digest", bad[("workflow_tools.yml", 36)][0])
        skipped = [r for r in rows if "skip" in r]
        self.assertEqual(len(skipped), 5)
        # Each digest costs one registry lookup.
        self.assertEqual(len(reg.calls), 3)

    def test_cannot_date_an_image(self):
        row = {"kind": "image", "uses": "ghcr.io/acme/app:1.0.0@sha256:" + "2" * 64}
        problems = pins.problems_for(row, pins.Resolver(registry()), 7, NOW)
        self.assertIn("cannot date the digest", problems[0])

    def test_unknown_tag(self):
        row = pins.parse_uses("w.yml", "uses: a/b@" + "c" * 40 + " # v9.9.9")[0]
        with mock.patch.object(_gh, "api", FakeApi({})):
            self.assertEqual(pins.problems_for(row, pins.Resolver(registry()), 7, NOW),
                             ["tag v9.9.9 not found in a/b"])

    def test_lightweight_tag_is_never_dated_by_its_commit(self):
        routes = tag("acme/act", "v1.0.0", "c" * 40)
        routes["repos/acme/act/commits/"] = commit("c" * 40, "2026-01-01T00:00:00Z")
        row = pins.parse_uses("w.yml", "uses: acme/act@" + "c" * 40 + " # v1.0.0")[0]
        with mock.patch.object(_gh, "api", FakeApi(routes)):
            problems = pins.problems_for(row, pins.Resolver(registry()), 7, NOW)
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot date", problems[0])
        self.assertIn("lightweight", problems[0])

    def test_resolver_caches_tags(self):
        api = FakeApi(tag_routes())
        with mock.patch.object(_gh, "api", api):
            r = pins.Resolver(registry())
            r.tag("actions/checkout", "v7.0.1")
            r.tag("actions/checkout", "v7.0.1")
        self.assertEqual(len(api.calls), 2)

    def test_main_exit_codes_and_json_before_subcommand(self):
        with mock.patch.object(_gh, "api", FakeApi(tag_routes())), \
                mock.patch.object(_registry, "Client", registry), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(pins.main(["check", WORKFLOW]), 1)
        self.assertIn("9 pins checked, 6 with problems, 0 not checked, 1 skipped.", out.getvalue())
        self.assertIn("skipped, local action", out.getvalue())
        with mock.patch.object(_gh, "api", FakeApi(tag_routes())), \
                mock.patch.object(_registry, "Client", registry), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(pins.main(["--json", "check", WORKFLOW]), 1)
        data = json.loads(out.getvalue())
        self.assertEqual((data["checked"], len(data["problems"]), len(data["skipped"])), (9, 6, 1))


class ResolveTest(unittest.TestCase):
    def test_pick_release_sorts_by_version_and_skips_young_moving_and_pre(self):
        rel = pins.pick_release(fixture_json("releases_action.json"), 7, NOW)
        self.assertEqual(rel["tag_name"], "v1.9.10")

    def test_pick_release_none_old_enough(self):
        self.assertIsNone(pins.pick_release(fixture_json("releases_action.json"), 400, NOW))

    def test_resolve_with_releases(self):
        api = FakeApi({"repos/acme/act/releases": fixture_json("releases_action.json"),
                       "repos/acme/act/commits/v1.9.10": commit("a" * 40)})
        with mock.patch.object(_gh, "api", api):
            res = pins.resolve("acme/act/sub", 7, NOW)
        self.assertEqual(res, {"action": "acme/act/sub", "tag": "v1.9.10", "sha": "a" * 40,
                               "published_at": "2026-09-01T00:00:00Z", "source": "release"})

    def test_resolve_falls_back_to_annotated_tags_only(self):
        routes = {
            "repos/acme/act/releases": [],
            "repos/acme/act/tags": [{"name": "v2.1.0", "commit": {"sha": "d" * 40}},
                                    {"name": "v2.0.0", "commit": {"sha": "b" * 40}},
                                    {"name": "v1.5.0", "commit": {"sha": "c" * 40}},
                                    {"name": "v2", "commit": {"sha": "b" * 40}}],
            # A commit date that would pass the cooldown. Sadr must not use it.
            "repos/acme/act/commits/": commit("d" * 40, "2026-01-01T00:00:00Z"),
        }
        routes.update(tag("acme/act", "v2.1.0", "d" * 40))
        routes["repos/acme/act/git/ref/tags/v2.0.0"] = {"object": {"type": "tag", "sha": "e" * 40}}
        routes["repos/acme/act/git/tags/" + "e" * 40] = {"tagger": {"date": "2026-10-21T00:00:00Z"},
                                                        "object": {"sha": "b" * 40}}
        routes["repos/acme/act/git/ref/tags/v1.5.0"] = {"object": {"type": "tag", "sha": "f" * 40}}
        routes["repos/acme/act/git/tags/" + "f" * 40] = {"tagger": {"date": "2026-08-01T00:00:00Z"},
                                                        "object": {"sha": "c" * 40}}
        with mock.patch.object(_gh, "api", FakeApi(routes)):
            res = pins.resolve("acme/act", 7, NOW)
        self.assertEqual((res["tag"], res["sha"], res["source"], res["undated"]),
                         ("v1.5.0", "c" * 40, "annotated tag", ["v2.1.0"]))

    def test_resolve_with_only_lightweight_tags(self):
        routes = {"repos/acme/act/releases": [],
                  "repos/acme/act/tags": [{"name": "v1.0.0", "commit": {"sha": "c" * 40}}]}
        routes.update(tag("acme/act", "v1.0.0", "c" * 40))
        with mock.patch.object(_gh, "api", FakeApi(routes)), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(pins.main(["resolve", "acme/act"]), 1)
        self.assertIn("cannot date these lightweight tags: v1.0.0", err.getvalue())


class ImageResolveTest(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(_registry.parse_ref("python:3.15-slim"),
                         {"registry": "docker.io", "repo": "library/python", "tag": "3.15-slim", "digest": None,
                          "official": True})
        p = _registry.parse_ref("ghcr.io/astral-sh/uv:0.13.1@" + UV_DIGEST)
        self.assertEqual((p["registry"], p["repo"], p["tag"], p["digest"], p["official"]),
                         ("ghcr.io", "astral-sh/uv", "0.13.1", UV_DIGEST, False))
        p = _registry.parse_ref("localhost:5000/app")
        self.assertEqual((p["registry"], p["repo"], p["tag"]), ("localhost:5000", "app", None))
        self.assertEqual(_registry.parse_ref("koreader/kosync")["repo"], "koreader/kosync")

    def test_official_image_from_repo_info_history(self):
        path = "repos/docker-library/repo-info/"
        md = "## `python:3.15-slim`\n\n```console\n$ docker pull python@{}\n```\n"
        old = "sha256:" + "a" * 64
        routes = {
            path + "commits?path=repos/python/remote/3.15-slim.md&until=2026-10-15T12:00:00Z": [
                {"sha": "c3", "commit": {"committer": {"date": "2026-10-15T08:00:00Z"}}},
                {"sha": "c2", "commit": {"committer": {"date": "2026-10-14T15:54:44Z"}}},
                {"sha": "c1", "commit": {"committer": {"date": "2026-10-13T00:00:00Z"}}}],
            path + "contents/repos/python/remote/3.15-slim.md?ref=c3": md.format(PY_DIGEST),
            path + "contents/repos/python/remote/3.15-slim.md?ref=c2": md.format(PY_DIGEST),
            path + "contents/repos/python/remote/3.15-slim.md?ref=c1": md.format(old),
        }
        with mock.patch.object(_gh, "api", FakeApi(routes)):
            res = pins.resolve_image("python:3.15-slim", 7, NOW)
        self.assertEqual(res["pin"], "python:3.15-slim@" + PY_DIGEST)
        self.assertEqual(res["published_at"], "2026-10-14T15:54:44Z")

    def test_other_registry_uses_config_created(self):
        reg = FakeRegistry({"0.13.1": "2026-10-14T03:30:59Z", "edge": "2026-10-21T00:00:00Z"})
        res = pins.resolve_image("ghcr.io/astral-sh/uv:0.13.1", 7, NOW, client=reg)
        self.assertEqual(res["pin"], "ghcr.io/astral-sh/uv:0.13.1@0.13.1")
        res = pins.resolve_image("ghcr.io/astral-sh/uv:edge", 7, NOW, client=reg)
        self.assertIsNone(res["digest"])
        self.assertIn("keeps no older digest", res["reason"])

    def test_registry_client_token_flow(self):
        index = {"manifests": [
            {"digest": "sha256:att", "platform": {"os": "unknown"}},
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]}
        responses = {
            "https://registry-1.docker.io/v2/library/python/manifests/3.15-slim": (index, {"Docker-Content-Digest": "sha256:idx"}),
            "https://registry-1.docker.io/v2/library/python/manifests/sha256:amd": ({"config": {"digest": "sha256:cfg"}}, {}),
            "https://registry-1.docker.io/v2/library/python/blobs/sha256:cfg": ({"created": "2026-10-01T00:00:00Z"}, {}),
            "https://auth.docker.io/token?service=registry.docker.io&scope=repository%3Alibrary%2Fpython%3Apull":
                ({"token": "anon"}, {}),
        }
        seen = []

        class Resp(io.BytesIO):
            def __init__(self, body, headers):
                super().__init__(json.dumps(body).encode())
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, req.get_header("Authorization")))
                if "registry-1" in req.full_url and not req.get_header("Authorization"):
                    import urllib.error
                    raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {
                        "WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'},
                        io.BytesIO(b""))
                return Resp(*responses[req.full_url])

        digest, created = _registry.Client(Opener()).created("docker.io", "library/python", "3.15-slim")
        self.assertEqual((digest, created), ("sha256:idx", "2026-10-01T00:00:00Z"))
        self.assertEqual(sum(1 for u, _ in seen if "auth.docker.io" in u), 1)
        self.assertTrue(all(a == "Bearer anon" for u, a in seen[2:] if "registry-1" in u))



class MoreCheckTest(unittest.TestCase):
    def test_go_install_and_scripts(self):
        text = ("go install golang.org/x/vuln/cmd/govulncheck@latest\n"
                "go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.9\n"
                "go run honnef.co/go/tools/cmd/staticcheck@2025.1\n"
                "go install ./...\ngo run main.go\ngo install golang.org/x/tools/cmd/goimports\n")
        rows = pins.parse_file("tools.sh", text)
        got = [(r["package"], r["version"], bool(r.get("problems")), "skip" in r) for r in rows]
        self.assertEqual(got, [("golang.org/x/vuln/cmd/govulncheck", "latest", True, False),
                               ("github.com/rhysd/actionlint/cmd/actionlint", "v1.7.9", False, False),
                               ("honnef.co/go/tools/cmd/staticcheck", "2025.1", True, False),
                               ("golang.org/x/tools/cmd/goimports", None, False, True)])
        self.assertEqual(len(pins.parse_file("Makefile", "lint:\n\tnpx eslint@latest .\n")), 1)

    def test_setup_uv_without_version(self):
        wf = ("jobs:\n  a:\n    steps:\n"
              "      - uses: astral-sh/setup-uv@" + "a" * 40 + " # v7.1.0\n"
              "        with:\n          enable-cache: true\n"
              "      - uses: astral-sh/setup-uv@" + "a" * 40 + " # v7.1.0\n"
              "        with:\n          version: 0.9.2\n"
              "      - uses: astral-sh/setup-uv@" + "a" * 40 + " # v7.1.0\n"
              "      - run: echo hi\n")
        rows = [r for r in pins.parse_setup_inputs("w.yml", wf)]
        missing = [r for r in rows if r["value"] is None]
        self.assertEqual([r["line"] for r in missing], [4, 10])
        self.assertIn("installs the newest release", missing[0]["problems"][0])
        self.assertEqual([r["value"] for r in rows if r["value"]], ["0.9.2"])

    def test_version_files(self):
        self.assertTrue(pins.parse_file(".python-version", "3.15\n")[0]["problems"])
        self.assertFalse(pins.parse_file(".python-version", "3.15.0\n")[0]["problems"])
        self.assertFalse(pins.parse_file(".nvmrc", "v24.11.0\n")[0]["problems"])
        self.assertTrue(pins.parse_file(".nvmrc", "lts/*\n")[0]["problems"])
        rows = pins.parse_file(".tool-versions", "nodejs 24.11.0\npython 3.15 # floating\nruby system\n")
        self.assertEqual([(r["uses"], bool(r.get("problems")), "skip" in r) for r in rows],
                         [("nodejs 24.11.0", False, False), ("python 3.15", True, False),
                          ("ruby system", False, True)])

    def test_rate_limited_image_is_not_a_problem(self):
        class Limited:
            def created(self, *a):
                raise _registry.RateLimited("registry-1.docker.io: HTTP 429")
        with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as f:
            f.write("FROM python:3.15-slim@" + PY_DIGEST + "\n")
        try:
            rows = pins.check([f.name], 7, resolver=pins.Resolver(Limited()), at=NOW)
        finally:
            os.unlink(f.name)
        self.assertEqual((rows[0]["problems"], "rate limited" in rows[0]["not_checked"]), ([], True))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(pins.print_check(rows, False), 3)
        self.assertIn("1 not checked", out.getvalue())

    def test_registry_retries_429(self):
        import urllib.error
        calls = []

        class Opener:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "2"},
                                             io.BytesIO(b""))
        waits = []
        with mock.patch.object(_registry, "sleep", waits.append):
            with self.assertRaises(_registry.RateLimited):
                _registry.Client(Opener()).created("ghcr.io", "a/b", "1.0.0")
        self.assertEqual((len(calls), waits), (4, [2, 2, 2]))
        self.assertEqual(_registry.retry_after({}, 1), 10)
        self.assertEqual(_registry.retry_after({"Retry-After": "999"}, 0), 60)


class NewestImageTest(unittest.TestCase):
    def test_tag_version(self):
        self.assertEqual(pins.tag_version("3.15.1-slim", "slim"), (3, 15, 1))
        self.assertIsNone(pins.tag_version("3.15.1-slim"))
        self.assertIsNone(pins.tag_version("3.15-slim", "slim"))
        self.assertEqual(pins.tag_version("v1.181.0"), (1, 181, 0))

    def test_walks_down_to_an_old_enough_digest(self):
        class Reg(FakeRegistry):
            def tags(self, registry, repo):
                return ["latest", "1.182.0", "1.181.0", "1.180.2", "1.181.0-nonroot", "canary"]
        reg = Reg({"1.182.0": "2026-10-21T00:00:00Z", "1.181.0": "2026-10-14T19:17:33Z",
                   "1.180.2": "2026-10-01T00:00:00Z"})
        res = pins.newest_image("ghcr.io/semgrep/semgrep", min_age=7, at=NOW, client=reg)
        self.assertEqual((res["tag"], res["pin"]), ("1.181.0", "ghcr.io/semgrep/semgrep:1.181.0@1.181.0"))
        self.assertEqual([p["tag"] for p in res["passed_over"]], ["1.182.0"])
        res = pins.newest_image("ghcr.io/semgrep/semgrep", line="1.180", min_age=7, at=NOW, client=reg)
        self.assertEqual(res["tag"], "1.180.2")
        res = pins.newest_image("ghcr.io/semgrep/semgrep", variant="alpine", client=reg, at=NOW)
        self.assertIsNone(res["digest"])


NODE_INDEX = [
    {"version": "v26.1.0", "date": "2026-10-21", "lts": False, "security": False},
    {"version": "v24.22.1", "date": "2026-10-20", "lts": "Krypton", "security": True},
    {"version": "v24.22.0", "date": "2026-10-01", "lts": "Krypton", "security": False},
    {"version": "v24.21.0", "date": "2026-09-10", "lts": "Krypton", "security": False},
    {"version": "v22.30.0", "date": "2026-09-01", "lts": "Jod", "security": False},
]


class RuntimeTest(unittest.TestCase):
    def releases(self):
        with mock.patch.object(pins, "FETCH", lambda url: json.dumps(NODE_INDEX)):
            return pins.node_releases()

    def test_floating_spec_never_pins_lower(self):
        res = pins.resolve_runtime("node", "24", 7, NOW, self.releases())
        self.assertEqual((res["pin"], res["pin_inside_cooldown"], res["pin_security"]), ("24.22.1", True, True))
        self.assertEqual(res["newest_past_cooldown"]["version"], "24.22.0")
        self.assertIn("never a lower one", res["note"])
        self.assertEqual(pins.resolve_runtime("node", "24.x", 7, NOW, self.releases())["pin"], "24.22.1")
        self.assertEqual(pins.resolve_runtime("node", "lts/*", 7, NOW, self.releases())["pin"], "24.22.1")
        self.assertEqual(pins.resolve_runtime("node", "lts/jod", 7, NOW, self.releases())["pin"], "22.30.0")

    def test_exact_spec_moves_up_only(self):
        res = pins.resolve_runtime("node", "24.21.0", 7, NOW, self.releases())
        self.assertEqual((res["pin"], res["newer_security_release"]), ("24.22.0", True))
        res = pins.resolve_runtime("node", "24.22.1", 7, NOW, self.releases())
        self.assertEqual(res["pin"], "24.22.1")
        with self.assertRaises(ValueError):
            pins.resolve_runtime("node", ">=24", 7, NOW, self.releases())
        with self.assertRaises(LookupError):
            pins.resolve_runtime("node", "30", 7, NOW, self.releases())

    def test_go_release_page(self):
        html = ("<h2>go1.27.0 (released 2026-08-19)</h2><p>Go 1.27.0 is a major release.</p>"
                "<p>go1.27.1\n (released 2026-09-01) includes fixes to the compiler.</p>"
                "<p>go1.27.2 (released 2026-10-08) includes\n<a>security</a>\nfixes to net/http.</p>"
                "<h2>go1.26.0 (released 2026-02-10)</h2>")
        with mock.patch.object(pins, "FETCH", lambda url: html):
            rel = pins.go_releases()
        self.assertEqual([(r["version"], r["security"]) for r in rel],
                         [("1.27.2", True), ("1.27.1", False), ("1.27.0", False), ("1.26.0", False)])
        res = pins.resolve_runtime("go", "stable", 7, NOW, rel)
        self.assertEqual((res["pin"], res["line"]), ("1.27.2", "1.27"))
        self.assertEqual(pins.resolve_runtime("go", "oldstable", 7, NOW, rel)["pin"], "1.26.0")
        self.assertEqual(pins.resolve_runtime("go", "go1.27", 7, NOW, rel)["pin"], "1.27.2")

    def test_main_runtime(self):
        rel = self.releases()
        with mock.patch.dict(pins.RUNTIMES, {"node": lambda: rel}), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(pins.main(["--json", "runtime", "node", "24"]), 0)
        self.assertEqual(json.loads(out.getvalue())["pin"], "24.22.1")


if __name__ == "__main__":
    unittest.main()
