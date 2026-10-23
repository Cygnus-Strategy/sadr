#!/usr/bin/env python3
"""Ask OSV (osv.dev) which published advisories affect a package version.

Sadr uses this to check that a version it keeps or picks has no published
advisory, for example a version that a cooldown would roll back to, or a
Go toolchain pin. A package is ECOSYSTEM:NAME@VERSION, with the OSV
ecosystem name: npm, PyPI, Go, crates.io, RubyGems, Maven, NuGet,
Packagist, Pub, Hex, SwiftURL. The Go toolchain and standard library is
go:stdlib@1.27.1, or `--runtime go 1.27.1`.

For each package the script prints the advisory ids, their aliases (CVE,
GHSA), a summary, and the versions that fix it on the package's ranges.
OSV covers no Node.js or CPython runtime releases, so --runtime takes
only go. The queries are read-only POSTs to api.osv.dev.

Exit codes: 0 no advisory, 1 at least one advisory, 2 an error.

Examples:
  osv.py npm:follow-redirects@1.16.0
  osv.py --json PyPI:pillow@12.3.0 Go:golang.org/x/net@v0.30.0
  osv.py --runtime go 1.27.1

Needs network access to api.osv.dev. Standard library only.
"""
import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.osv.dev/v1/query"
ECOSYSTEMS = {e.lower(): e for e in ("npm", "PyPI", "Go", "crates.io", "RubyGems", "Maven", "NuGet", "Packagist",
                                     "Pub", "Hex", "SwiftURL", "Hackage", "CRAN", "GitHub Actions")}
# Tests replace these.
sleep = time.sleep


def post(body, attempts=3):
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "sadr"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise LookupError(f"OSV: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == attempts - 1:
                raise LookupError(f"OSV: {getattr(e, 'reason', e)}") from None
        sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def parse_spec(spec):
    """'npm:@scope/pkg@1.2.3' -> ('npm', '@scope/pkg', '1.2.3')."""
    eco, sep, rest = spec.partition(":")
    at = rest.rfind("@")
    if not sep or at <= 0:
        raise ValueError(f"expected ECOSYSTEM:NAME@VERSION, not {spec!r}")
    name, version = rest[:at], rest[at + 1:]
    eco = "Go" if eco.lower() == "go" else ECOSYSTEMS.get(eco.lower(), eco)
    if eco == "Go":
        version = re.sub(r"^(go|v)", "", version)
    return eco, name, version


def fixed_versions(vuln, eco, name):
    out = []
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        if pkg.get("ecosystem") != eco or pkg.get("name") != name:
            continue
        for rng in aff.get("ranges") or []:
            out += [ev["fixed"] for ev in rng.get("events") or [] if ev.get("fixed")]
    return list(dict.fromkeys(out))


def query(eco, name, version, fetch=None):
    """Return the advisories that affect NAME@VERSION in ECO, all pages."""
    fetch = fetch or post
    vulns, token = [], None
    while True:
        body = {"package": {"ecosystem": eco, "name": name}, "version": version}
        if token:
            body["page_token"] = token
        data = fetch(body)
        vulns += data.get("vulns") or []
        token = data.get("next_page_token")
        if not token:
            break
    rows = []
    for v in vulns:
        rows.append({"id": v.get("id"), "aliases": v.get("aliases") or [],
                     "summary": (v.get("summary") or v.get("details") or "")[:200],
                     "fixed": fixed_versions(v, eco, name), "published": v.get("published"),
                     "modified": v.get("modified")})
    rows.sort(key=lambda r: r["id"] or "")
    return {"ecosystem": eco, "package": name, "version": version, "vulns": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("packages", nargs="*", metavar="ECOSYSTEM:NAME@VERSION")
    ap.add_argument("--runtime", nargs=2, metavar=("NAME", "VERSION"), help="a runtime release: go 1.27.1")
    ap.add_argument("--json", action="store_true", help="print JSON")
    a = ap.parse_args(argv)
    specs = []
    try:
        specs = [parse_spec(p) for p in a.packages]
        if a.runtime:
            if a.runtime[0].lower() != "go":
                ap.error(f"OSV has no {a.runtime[0]} runtime releases. Only go works with --runtime")
            specs.append(("Go", "stdlib", re.sub(r"^(go|v)", "", a.runtime[1])))
    except ValueError as e:
        ap.error(str(e))
    if not specs:
        ap.error("pass at least one ECOSYSTEM:NAME@VERSION or --runtime go VERSION")
    try:
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            results = list(ex.map(lambda s: query(*s), specs))
    except LookupError as e:
        print(f"osv.py: {e}", file=sys.stderr)
        return 2
    if a.json:
        json.dump(results, sys.stdout, indent=2)
        print()
    else:
        for r in results:
            head = f"{r['ecosystem']}:{r['package']}@{r['version']}"
            if not r["vulns"]:
                print(f"{head}: no published advisory")
            for v in r["vulns"]:
                ids = ", ".join([v["id"]] + [x for x in v["aliases"] if x != v["id"]][:3])
                fixed = ", ".join(v["fixed"]) or "no fixed version"
                print(f"{head}: {ids}: fixed in {fixed}. {v['summary']}")
    return 1 if any(r["vulns"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
