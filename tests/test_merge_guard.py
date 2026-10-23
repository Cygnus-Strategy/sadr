import io
import json
import unittest
from unittest import mock

import helpers  # noqa: F401
import _gh
import merge_guard

FREE_PRIVATE = ("gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)")


def fake_run(answers):
    """Stand-in for _gh.run: maps an API path to JSON data or to an error text."""
    def run(args, retries=None):
        path = args[-1]
        resp = answers.get(path)
        if resp is None:
            raise _gh.GhError(f"gh api {path}: gh: Not Found (HTTP 404)")
        if isinstance(resp, str):
            raise _gh.GhError(f"gh api {path}: {resp}")
        return json.dumps(resp)
    return run


REPO = {"default_branch": "main", "private": False, "allow_auto_merge": True, "allow_merge_commit": False,
        "allow_squash_merge": True, "allow_rebase_merge": False}


class GuardTest(unittest.TestCase):
    def test_free_private_repo_cannot_verify(self):
        # Recorded from acme/site: both endpoints answer 403 on a private repo of a free plan.
        answers = {"repos/acme/app": {**REPO, "private": True, "allow_auto_merge": False},
                   "repos/acme/app/branches/main/protection/required_status_checks": FREE_PRIVATE,
                   "repos/acme/app/rules/branches/main": FREE_PRIVATE}
        with mock.patch.object(_gh, "run", fake_run(answers)):
            g = merge_guard.guard("acme/app")
        self.assertIsNone(g["required_checks"])
        self.assertIn("HTTP 403", g["cannot_verify"])
        self.assertIn("Upgrade to GitHub Pro", g["cannot_verify"])
        self.assertFalse(g["eligible"])

    def test_unprotected_branch_has_zero_checks(self):
        answers = {"repos/acme/app": REPO,
                   "repos/acme/app/branches/main/protection/required_status_checks": "gh: Branch not protected (HTTP 404)",
                   "repos/acme/app/rules/branches/main": []}
        with mock.patch.object(_gh, "run", fake_run(answers)):
            g = merge_guard.guard("acme/app")
        self.assertEqual((g["required_checks"], g["cannot_verify"], g["eligible"]), (0, None, False))
        self.assertIn("main requires no status check", g["why_not"])

    def test_other_404_cannot_verify(self):
        answers = {"repos/acme/app": REPO, "repos/acme/app/rules/branches/main": []}
        with mock.patch.object(_gh, "run", fake_run(answers)):
            g = merge_guard.guard("acme/app")
        self.assertIn("Not Found (HTTP 404)", g["cannot_verify"])
        self.assertFalse(g["eligible"])

    def test_checks_from_protection_and_rulesets(self):
        answers = {"repos/acme/app": REPO,
                   "repos/acme/app/branches/main/protection/required_status_checks": {
                       "contexts": ["test"], "checks": [{"context": "test"}, {"context": "lint"}]},
                   "repos/acme/app/rules/branches/main": [
                       {"type": "deletion"},
                       {"type": "required_status_checks",
                        "parameters": {"required_status_checks": [{"context": "semgrep"}]}}]}
        with mock.patch.object(_gh, "run", fake_run(answers)), mock.patch("sys.stdout", io.StringIO()) as out:
            merge_guard.main(["--repo", "acme/app"])
        g = json.loads(out.getvalue())
        self.assertEqual((g["required_checks"], g["required_check_names"]), (3, ["lint", "semgrep", "test"]))
        self.assertEqual(g["merge_methods"], ["squash"])
        self.assertTrue(g["eligible"])
        self.assertEqual(g["why_not"], [])

    def test_missing_repo(self):
        with mock.patch.object(_gh, "run", fake_run({})):
            g = merge_guard.guard("acme/gone")
        self.assertFalse(g["eligible"])
        self.assertIn("HTTP 404", g["cannot_verify"])


class KindTest(unittest.TestCase):
    def test_bump_levels(self):
        cases = [("1.2.3", "1.2.4", False, "patch"), ("1.2.3", "1.3.0", False, "minor"),
                 ("1.2.3", "2.0.0", False, "major"), ("0.143.0", "0.148.0", False, "major"),
                 ("0.3.1", "0.3.2", False, "patch"), ("0.0.3", "0.0.4", False, "major"),
                 ("v4.1", "v4.2", False, "minor"), ("3.14.2", "3.15.0", True, "major"),
                 ("24.11.0", "24.11.1", True, "patch"), ("2.0.0", "1.9.0", False, "major"),
                 ("1.0.0-rc.1", "1.0.0", False, "major"), ("1.2.3", "1.2.3", False, "none")]
        for old, new, runtime, want in cases:
            self.assertEqual(merge_guard.bump_level(old, new, runtime), want, (old, new))

    def test_parse_bump(self):
        b = merge_guard.parse_bump("runtime:python=3.14.2..3.15.0")
        self.assertEqual((b["name"], b["runtime"], b["level"]), ("runtime:python", True, "major"))
        with self.assertRaises(ValueError):
            merge_guard.parse_bump("vite 7.1.0 7.2.0")

    def test_settings_sentences(self):
        parse = merge_guard.parse_settings
        self.assertEqual(parse(["Auto-merge facts and housekeeping PRs. Enhancement PRs need review."])[0],
                         {"facts": "on", "upkeep": "all", "issue": "off"})
        self.assertEqual(parse(["Auto-merge minor updates."])[0], {"facts": "off", "upkeep": "minor", "issue": "off"})
        self.assertEqual(parse(["Auto-merge everything.", "Enhancement PRs need review."])[0],
                         {"facts": "on", "upkeep": "all", "issue": "off"})
        self.assertEqual(parse(["Auto-merge everything.", "Don't auto-merge."])[0],
                         {"facts": "off", "upkeep": "off", "issue": "off"})
        self.assertEqual(parse(["Auto-merge patch updates and facts PRs."])[0],
                         {"facts": "on", "upkeep": "patch", "issue": "off"})
        settings, unclear = parse(["Auto-merge sometimes."])
        self.assertEqual((settings["upkeep"], unclear), ("off", ["Auto-merge sometimes."]))
        self.assertEqual(parse([""])[0], {"facts": "off", "upkeep": "off", "issue": "off"})

    def test_off_makes_no_call(self):
        def boom(repo):
            raise AssertionError("no API call when the setting is off")
        res = merge_guard.decide("acme/app", "facts", "off", guard_fn=boom)
        self.assertEqual((res["eligible"], res["guard_checked"]), (False, False))

    def test_upkeep_level_blocks_bigger_bumps(self):
        ok = {"repo": "acme/app", "eligible": True, "why_not": []}
        bumps = [merge_guard.parse_bump("vite=7.1.0..7.2.1"), merge_guard.parse_bump("zod=0.3.0..0.4.0")]
        res = merge_guard.decide("acme/app", "upkeep", "minor", bumps, guard_fn=lambda r: ok)
        self.assertFalse(res["eligible"])
        self.assertIn("zod 0.3.0 to 0.4.0 is a major change", res["why_not"][0])
        res = merge_guard.decide("acme/app", "upkeep", "all", bumps, guard_fn=lambda r: ok)
        self.assertTrue(res["eligible"])
        res = merge_guard.decide("acme/app", "issue", "on", guard_fn=lambda r: ok)
        self.assertTrue(res["eligible"])

    def test_owner_then_repo_sections_with_sources(self):
        owner = "### Settings\nAuto-merge everything.\n\n### Build and test\n- Don't auto-merge anything that fails.\n"
        repo = "### Build and test\n- make test\n\n### Settings\n- Enhancement PRs need review. Don't auto-merge enhancement PRs.\n"
        self.assertEqual(merge_guard.settings_part(owner).strip(), "Auto-merge everything.")
        self.assertEqual(merge_guard.settings_part("Auto-merge facts PRs."), "Auto-merge facts PRs.")
        settings, unclear, sources = merge_guard.resolve_settings([owner, repo])
        self.assertEqual(settings, {"facts": "on", "upkeep": "all", "issue": "off"})
        self.assertEqual(sources, {"facts": "owner", "upkeep": "owner", "issue": "repo"})
        self.assertEqual(unclear, [])
        settings, _, sources = merge_guard.resolve_settings(["", ""])
        self.assertEqual((settings["facts"], sources["facts"]), ("off", "default"))
        settings, _, sources = merge_guard.resolve_settings(["", "### Settings\nAuto-merge facts PRs.\n"])
        self.assertEqual((settings["facts"], sources["facts"], sources["upkeep"]), ("on", "repo", "default"))

    def test_main_with_kind(self):
        answers = {"repos/acme/app": REPO,
                   "repos/acme/app/branches/main/protection/required_status_checks": {"contexts": ["test"]},
                   "repos/acme/app/rules/branches/main": []}
        with mock.patch.object(_gh, "run", fake_run(answers)), mock.patch("sys.stdout", io.StringIO()) as out:
            merge_guard.main(["--repo", "acme/app", "--kind", "upkeep", "--settings-text",
                              "Auto-merge patch updates.", "--bump", "vite=7.1.0..7.1.1"])
        res = json.loads(out.getvalue())
        self.assertEqual((res["kind"], res["setting"], res["eligible"]), ("upkeep", "patch", True))
        self.assertEqual(res["setting_source"], "settings text 1")
        with mock.patch.object(_gh, "run", fake_run(answers)), mock.patch("sys.stdout", io.StringIO()) as out:
            merge_guard.main(["--repo", "acme/app", "--kind", "facts", "--settings-text",
                              "### Settings\nAuto-merge everything.", "--settings-text", ""])
        res = json.loads(out.getvalue())
        self.assertEqual((res["setting"], res["setting_source"], res["eligible"]), ("on", "owner", True))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            merge_guard.main(["classify", "0.143.0", "0.148.0"])
        self.assertEqual(out.getvalue().strip(), "major")
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            merge_guard.main(["--repo", "acme/app", "--kind", "facts", "--setting", "minor"])


if __name__ == "__main__":
    unittest.main()
