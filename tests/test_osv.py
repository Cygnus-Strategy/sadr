import io
import json
import unittest
from unittest import mock

import helpers  # noqa: F401
import osv

VULN = {"id": "GO-2026-6809", "aliases": ["CVE-2026-111943"], "summary": "Distrust certificates",
        "affected": [{"package": {"ecosystem": "Go", "name": "stdlib"},
                      "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.26.10"},
                                                               {"introduced": "1.27.0-0"}, {"fixed": "1.27.3"}]}]},
                     {"package": {"ecosystem": "Go", "name": "other"},
                      "ranges": [{"events": [{"fixed": "9.9.9"}]}]}]}


class OsvTest(unittest.TestCase):
    def test_parse_spec(self):
        self.assertEqual(osv.parse_spec("npm:@scope/pkg@1.2.3"), ("npm", "@scope/pkg", "1.2.3"))
        self.assertEqual(osv.parse_spec("pypi:pillow@12.3.0"), ("PyPI", "pillow", "12.3.0"))
        self.assertEqual(osv.parse_spec("go:stdlib@go1.27.1"), ("Go", "stdlib", "1.27.1"))
        self.assertEqual(osv.parse_spec("Go:golang.org/x/net@v0.30.0"), ("Go", "golang.org/x/net", "0.30.0"))
        with self.assertRaises(ValueError):
            osv.parse_spec("pillow@12")

    def test_query_pages_and_fixed_versions(self):
        pages = [{"vulns": [VULN], "next_page_token": "t"}, {"vulns": []}]
        bodies = []

        def fetch(body):
            bodies.append(body)
            return pages[len(bodies) - 1]
        res = osv.query("Go", "stdlib", "1.27.2", fetch)
        self.assertEqual(res["vulns"][0]["fixed"], ["1.26.10", "1.27.3"])
        self.assertEqual(bodies[1]["page_token"], "t")

    def test_main_exit_codes(self):
        with mock.patch.object(osv, "post", return_value={"vulns": [VULN]}), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(osv.main(["--runtime", "go", "go1.27.2"]), 1)
        self.assertIn("Go:stdlib@1.27.2: GO-2026-6809, CVE-2026-111943: fixed in 1.26.10, 1.27.3", out.getvalue())
        with mock.patch.object(osv, "post", return_value={}), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(osv.main(["--json", "npm:x@1.0.0"]), 0)
        self.assertEqual(json.loads(out.getvalue())[0]["vulns"], [])
        with mock.patch.object(osv, "post", side_effect=LookupError("OSV: HTTP 500")), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(osv.main(["npm:x@1.0.0"]), 2)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            osv.main(["--runtime", "node", "24.1.0"])


if __name__ == "__main__":
    unittest.main()
