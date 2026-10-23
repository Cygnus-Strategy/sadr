#!/usr/bin/env python3
"""Date every entry in lockfiles, and list the entries younger than the cooldown.

A cooldown setting in a package manager guards only the next resolve. An
entry that is already in a lockfile can be younger than the cooldown, for
example after a run without the setting. This script dates every entry,
not only the entries in a diff. It reads:

- npm package-lock.json and npm-shrinkwrap.json, pnpm-lock.yaml,
  yarn.lock (v1 and Berry), bun.lock, and deno.lock (npm and jsr entries)
- uv.lock, uv script locks (*.py.lock) and poetry.lock
- go.sum, Cargo.lock, and SwiftPM Package.resolved
- Nix flake.lock and devenv.lock

It dates each entry by its publish time in its registry: npm, jsr.io,
PyPI (or the upload-time in uv.lock), crates.io, and the GitHub release or
annotated tag of a Swift package. For Go, the module proxy gives the time
of the version's commit, and the script says so. An entry from git, a
private registry, a URL, or a lightweight tag cannot be dated. The script
lists those entries separately. Local and workspace entries are skipped.

For a Nix input, the date is lastModified, the time of the input's
commit, which its committer sets; the script says so.

A PATH can be a lockfile or a folder. In a folder the script finds the
lockfiles and skips node_modules, .git, vendor, .venv and build folders.
A named file that the script cannot read, such as an ESP-IDF
dependencies.lock, is skipped and listed under unread_files.

Exit codes: 0 when every entry is old enough or cannot be dated, 1 when
an entry is younger than --min-age-days, 2 on an error (a missing file,
no readable lockfile, or a lockfile that does not parse).

Examples:
  lockage.py go.sum
  lockage.py --min-age-days 7 .
  lockage.py --json web/package-lock.json uv.lock

Needs network access to the registries. Swift packages also need an
authenticated `gh` CLI. Standard library only.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import _gh

DEFAULT_MIN_AGE = 7
SKIP_DIRS = {"node_modules", ".git", "vendor", ".venv", "venv", ".build", "build", "dist", "target", ".tox",
             "DerivedData", "Pods", "third_party"}
NPM = "https://registry.npmjs.org"
PYPI = "https://pypi.org"
GOPROXY = "https://proxy.golang.org"
CRATES = "https://crates.io"
JSR = "https://jsr.io"


def lockfile_kind(path):
    """The lockfile format of PATH by its name, or None."""
    base = os.path.basename(path)
    if base in ("package-lock.json", "npm-shrinkwrap.json"):
        return "npm"
    if base == "pnpm-lock.yaml":
        return "pnpm"
    if base == "yarn.lock":
        return "yarn"
    if base in ("bun.lock", "bun.lockb"):
        return "bun"
    if base == "deno.lock":
        return "deno"
    if base == "uv.lock" or base.endswith(".py.lock"):
        return "uv"
    if base == "poetry.lock":
        return "poetry"
    if base == "go.sum":
        return "go"
    if base == "Cargo.lock":
        return "cargo"
    if base == "Package.resolved":
        return "swift"
    if base in ("flake.lock", "devenv.lock"):
        return "nix"
    return None


def find_lockfiles(paths):
    """Expand folders in PATHS into the lockfiles inside them."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".xcarchive"))
                out += [os.path.join(root, f) for f in sorted(files) if lockfile_kind(f)]
        else:
            out.append(p)
    return out


def entry(eco, name, version, **kw):
    return {"ecosystem": eco, "name": name, "version": version, **kw}


def split_at_version(spec):
    """'@scope/name@1.2.3' -> ('@scope/name', '1.2.3')."""
    at = spec.rfind("@")
    return (spec[:at], spec[at + 1:]) if at > 0 else (spec, "")


def split_peer_key(key):
    """A deno or pnpm key with peer suffixes: 'react-dom@19.3.1_react@19.3.1' -> ('react-dom', '19.3.1')."""
    m = re.match(r"^(@?[^@]+)@([^_(]+)", key)
    return (m.group(1), m.group(2)) if m else split_at_version(key)


# Parsers. Each returns a list of entries. An entry with "skip" is local.
# An entry with "undated" cannot be dated, and the value says why.

def parse_npm(text):
    data = json.loads(text)
    out = []
    if "packages" in data:
        for key, meta in data["packages"].items():
            if not key or meta.get("link"):
                continue
            name = meta.get("name") or key.rsplit("node_modules/", 1)[-1]
            if "node_modules/" not in key:
                # A workspace package, such as packages/local.
                out.append(entry("npm", name, meta.get("version", ""), skip="local"))
                continue
            out.append(npm_entry(name, meta.get("version", ""), meta.get("resolved")))
        return out

    def walk(deps):
        for name, meta in (deps or {}).items():
            out.append(npm_entry(name, meta.get("version", ""), meta.get("resolved")))
            walk(meta.get("dependencies"))
    walk(data.get("dependencies"))
    return out


def npm_entry(name, version, resolved=None):
    if version.startswith(("file:", "link:", "workspace:")) or (resolved or "").startswith("file:"):
        return entry("npm", name, version, skip="local")
    if version.startswith(("git", "github:", "http")) or (resolved or "").startswith("git"):
        return entry("npm", name, version, undated="from git or a URL, not a registry")
    if resolved and not resolved.startswith(("https://registry.npmjs.org/", "https://registry.yarnpkg.com/")):
        return entry("npm", name, version, undated=f"from another registry ({urllib.parse.urlsplit(resolved).netloc})")
    return entry("npm", name, version)


def parse_pnpm(text):
    out, section = [], None
    for line in text.splitlines():
        if re.match(r"^\S", line):
            section = line.rstrip(":").strip()
            continue
        if section != "packages":
            continue
        m = re.match(r"^  (['\"]?)(/?[^'\"\s]+)\1:\s*$", line)
        if not m:
            continue
        key = m.group(2).lstrip("/")
        if "@" in key[1:]:
            name, version = split_peer_key(key)
        else:
            # pnpm v5 keys: /name/1.2.3
            name, _, version = key.rpartition("/")
        if version.startswith(("link:", "file:", "workspace:")):
            out.append(entry("npm", name, version, skip="local"))
        elif re.match(r"^\d", version):
            out.append(npm_entry(name, version))
        else:
            out.append(entry("npm", name, version, undated="not a registry version"))
    return out


def parse_yarn(text):
    out, header = [], None
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            header = line.rstrip(":").strip()
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)
        if header and m:
            first = header.split(",")[0].strip().strip('"')
            if first == "__metadata":
                header = None
                continue
            name, spec = split_at_version(first)
            spec = spec.split(":", 1)[0] + ":" if ":" in spec else ""
            version = m.group(1)
            if spec in ("workspace:", "link:", "portal:", "file:"):
                out.append(entry("npm", name, version, skip="local"))
            elif spec in ("", "npm:", "patch:"):
                out.append(npm_entry(name, version))
            else:
                out.append(entry("npm", name, version, undated=f"from {spec.rstrip(':')}, not a registry"))
            header = None
    return out


def parse_bun(text, binary=False):
    if binary:
        return [entry("npm", "bun.lockb", "", undated="a binary lockfile. Save a text bun.lock to date it")]
    data = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
    out = []
    for meta in (data.get("packages") or {}).values():
        if not isinstance(meta, list) or not meta:
            continue
        name, version = split_at_version(meta[0])
        if version.startswith(("workspace:", "file:", "link:")):
            out.append(entry("npm", name, version, skip="local"))
        elif version.startswith(("github:", "git", "http")):
            out.append(entry("npm", name, version, undated="from git or a URL, not a registry"))
        else:
            out.append(npm_entry(name, version))
    return out


def parse_deno(text):
    data = json.loads(text)
    out = []
    for key in data.get("npm") or {}:
        name, version = split_peer_key(key)
        out.append(npm_entry(name, version))
    for key in data.get("jsr") or {}:
        name, version = split_at_version(key)
        out.append(entry("jsr", name, version))
    for url in data.get("remote") or {}:
        out.append(entry("url", url, "", undated="a remote module, with no publish date"))
    return out


def toml_blocks(text, header):
    """Yield the top-level 'key = value' lines of each [[HEADER]] block as a dict of raw values."""
    block = None
    for line in text.splitlines():
        if line.startswith("["):
            if block is not None:
                yield block
            block = {} if line.strip() == f"[[{header}]]" else None
            if block is None and line.strip() == f"[{header}.source]":
                block = {"__source_table__": True}
            continue
        if block is None:
            continue
        m = re.match(r'^([\w-]+)\s*=\s*(.*)$', line)
        if m:
            block.setdefault(m.group(1), m.group(2).strip())
        if "upload-time" in line:
            block.setdefault("__uploads__", []).extend(re.findall(r'upload-time\s*=\s*"([^"]+)"', line))
    if block is not None:
        yield block


def unquote(v):
    v = (v or "").strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def parse_uv(text):
    out = []
    for b in toml_blocks(text, "package"):
        name, version, source = unquote(b.get("name")), unquote(b.get("version")), b.get("source", "")
        if not name:
            continue
        if re.search(r"\b(editable|virtual|directory|path)\s*=", source):
            out.append(entry("pypi", name, version, skip="local"))
        elif "git" in source and "registry" not in source:
            out.append(entry("pypi", name, version, undated="from git, not a registry"))
        elif re.search(r"\burl\s*=", source):
            out.append(entry("pypi", name, version, undated="from a URL, not a registry"))
        elif "registry" in source and "pypi.org" not in source:
            out.append(entry("pypi", name, version, undated=f"from another registry ({unquote(source)})"))
        else:
            ups = sorted(b.get("__uploads__") or [])
            out.append(entry("pypi", name, version, **({"published_at": ups[0], "date_source": "uv.lock upload-time"}
                                                       if ups else {})))
    return out


def parse_poetry(text):
    out, last = [], None
    for b in toml_blocks(text, "package"):
        if b.get("__source_table__"):
            if last is not None:
                kind = unquote(b.get("type"))
                if kind in ("directory", "file"):
                    last["skip"] = "local"
                elif kind:
                    last["undated"] = f"from {kind} source {unquote(b.get('url'))}"
            continue
        last = entry("pypi", unquote(b.get("name")), unquote(b.get("version")))
        out.append(last)
    return out


def parse_go(text):
    built, mod_only = {}, {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        mod, version = parts[0], parts[1]
        if version.endswith("/go.mod"):
            mod_only[(mod, version[:-len("/go.mod")])] = True
        else:
            built[(mod, version)] = True
    out = [entry("go", m, v) for m, v in built]
    out += [entry("go", m, v, skip="go.mod hash only, not built") for m, v in mod_only if (m, v) not in built]
    return out


def parse_cargo(text):
    out = []
    for b in toml_blocks(text, "package"):
        name, version, source = unquote(b.get("name")), unquote(b.get("version")), unquote(b.get("source"))
        if not source:
            out.append(entry("crates", name, version, skip="local"))
        elif source.startswith("git+"):
            out.append(entry("crates", name, version, undated="from git, not a registry"))
        elif "crates.io-index" in source:
            out.append(entry("crates", name, version))
        else:
            out.append(entry("crates", name, version, undated=f"from another registry ({source})"))
    return out


def parse_swift(text):
    data = json.loads(text)
    pins = data.get("pins") or (data.get("object") or {}).get("pins") or []
    out = []
    for p in pins:
        url = p.get("location") or p.get("repositoryURL") or ""
        name = p.get("identity") or p.get("package") or url
        state = p.get("state") or {}
        version = state.get("version") or ""
        m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url)
        if (p.get("kind") or "remoteSourceControl") in ("localSourceControl", "fileSystem"):
            out.append(entry("swift", name, version, skip="local"))
        elif not version:
            out.append(entry("swift", name, state.get("revision", "")[:12],
                             undated=f"pinned to {'branch ' + state['branch'] if state.get('branch') else 'a revision'}, "
                                     "not a release"))
        elif not m:
            out.append(entry("swift", name, version, undated=f"not on GitHub ({url})"))
        else:
            out.append(entry("swift", name, version, repo=m.group(1)))
    return out


def parse_nix(text):
    """Inputs of a flake.lock or devenv.lock, dated by lastModified (a commit time)."""
    data = json.loads(text)
    root = (data.get("nodes") or {}).get(data.get("root") or "root") or {}
    out = []
    for key, node in sorted((data.get("nodes") or {}).items()):
        locked = node.get("locked")
        if not locked or node is root:
            continue
        kind = locked.get("type")
        if kind in ("github", "gitlab", "sourcehut"):
            name = f"{kind}:{locked.get('owner')}/{locked.get('repo')}"
        else:
            name = locked.get("url") or locked.get("path") or key
        version = (locked.get("rev") or locked.get("narHash") or "")[:12]
        if kind == "path":
            out.append(entry("nix", name, version, skip="a local path"))
        elif locked.get("lastModified"):
            when = datetime.datetime.fromtimestamp(int(locked["lastModified"]), datetime.timezone.utc)
            out.append(entry("nix", name, version, published_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             date_source="lastModified, the commit time that its committer sets"))
        else:
            out.append(entry("nix", name, version, undated="no lastModified in the lock"))
    return out


PARSERS = {"nix": parse_nix, "npm": parse_npm, "pnpm": parse_pnpm, "yarn": parse_yarn, "deno": parse_deno, "uv": parse_uv,
           "poetry": parse_poetry, "go": parse_go, "cargo": parse_cargo, "swift": parse_swift}


def parse(path):
    """Return the entries of one lockfile, each with its file."""
    kind = lockfile_kind(path)
    if kind == "bun" and path.endswith(".lockb"):
        rows = parse_bun("", binary=True)
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        rows = parse_bun(text) if kind == "bun" else PARSERS[kind](text)
    seen, out = set(), []
    for r in rows:
        key = (r["ecosystem"], r["name"], r["version"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": path, **r})
    return out


class Dater:
    """Look up publish times, with one cache per ecosystem and name."""

    def __init__(self, fetch=None):
        self.fetch = fetch or http_json
        self.cache = {}
        self.lock = threading.Lock()

    def cached(self, key, fn):
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        try:
            val = fn()
        except LookupError as e:
            val = e
        with self.lock:
            self.cache[key] = val
        return val

    def date(self, e):
        """Return (published_at, source) or raise LookupError with the reason."""
        eco, name, version = e["ecosystem"], e["name"], e["version"]
        if eco == "npm":
            doc = self.cached(("npm", name), lambda: self.fetch(f"{NPM}/{urllib.parse.quote(name, safe='@')}"))
            if isinstance(doc, Exception):
                raise doc
            when = (doc.get("time") or {}).get(version)
            if not when:
                raise LookupError(f"npm has no publish time for {name}@{version}")
            return when, "npm registry"
        if eco == "jsr":
            scope, _, pkg = name.lstrip("@").partition("/")
            doc = self.cached(("jsr", name), lambda: self.fetch(f"{JSR}/@{scope}/{pkg}/meta.json"))
            if isinstance(doc, Exception):
                raise doc
            when = ((doc.get("versions") or {}).get(version) or {}).get("createdAt")
            if not when:
                raise LookupError(f"jsr has no publish time for {name}@{version}")
            return when, "jsr.io"
        if eco == "pypi":
            doc = self.fetch(f"{PYPI}/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json")
            times = sorted(u.get("upload_time_iso_8601") for u in doc.get("urls") or [] if u.get("upload_time_iso_8601"))
            if not times:
                raise LookupError(f"PyPI has no upload time for {name} {version}")
            return times[0], "PyPI"
        if eco == "go":
            doc = self.fetch(f"{GOPROXY}/{go_escape(name)}/@v/{go_escape(version)}.info")
            if not doc.get("Time"):
                raise LookupError(f"the Go module proxy has no time for {name} {version}")
            return doc["Time"], "Go module proxy (the commit time, which its author sets)"
        if eco == "crates":
            doc = self.fetch(f"{CRATES}/api/v1/crates/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}")
            when = (doc.get("version") or {}).get("created_at")
            if not when:
                raise LookupError(f"crates.io has no publish time for {name} {version}")
            return when, "crates.io"
        if eco == "swift":
            for tag in (version, "v" + version):
                try:
                    sha, when, source = _gh.tag_date(e["repo"], tag)
                except _gh.GhError as err:
                    raise LookupError(str(err)) from None
                if sha:
                    if not when:
                        raise LookupError(source)
                    return when, f"GitHub {source}"
            raise LookupError(f"no tag {version} in {e['repo']}")
        raise LookupError(f"no registry for {eco}")


def go_escape(s):
    """Escape a module path or version for the Go module proxy: uppercase becomes !lowercase."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), s)


def http_json(url, attempts=3):
    """GET a JSON document over https. Raise LookupError on a 404 or a failure."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing a non-https URL: {url}")
    last = None
    for _ in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "sadr (https://sadr.cygnusstrategy.com)",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return json.loads(f.read())
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (404, 410):
                raise LookupError(f"{url}: HTTP {e.code}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = str(getattr(e, "reason", e))
    raise LookupError(f"{url}: {last}")


def date_all(entries, dater=None, at=None, workers=16):
    """Fill published_at, age_days and status (young, ok, undated, skipped) in each entry."""
    dater = dater or Dater()
    at = at or _gh.now()

    def one(e):
        if "skip" in e:
            e["status"] = "skipped"
            return e
        if "undated" in e:
            e["status"] = "undated"
            return e
        if not e.get("published_at"):
            try:
                e["published_at"], e["date_source"] = dater.date(e)
            except LookupError as err:
                e["status"], e["undated"] = "undated", str(err)
                return e
        e["age_days"] = round(_gh.age_days(e["published_at"], at), 2)
        return e

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, entries))


def classify(entries, min_age):
    for e in entries:
        if "status" not in e:
            e["status"] = "young" if e["age_days"] < min_age else "ok"
    return entries


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("paths", nargs="+", metavar="PATH", help="a lockfile, or a folder to search for lockfiles")
    ap.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE,
                    help=f"the cooldown in days (default: {DEFAULT_MIN_AGE})")
    ap.add_argument("--all", action="store_true", help="with --json, also print the entries that are old enough")
    _gh.add_json(ap)
    a = ap.parse_args(argv)
    try:
        return run(a)
    except Exception as e:  # noqa: BLE001 - any failure is exit 2, never a false "ok"
        print(f"lockage.py: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def run(a):
    files = find_lockfiles(a.paths)
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        print(f"lockage.py: no such file: {', '.join(missing)}", file=sys.stderr)
        return 2
    unknown = [f for f in files if not lockfile_kind(f)]
    files = [f for f in files if lockfile_kind(f)]
    if unknown:
        print(f"lockage.py: skipped, not a lockfile that this script reads: {', '.join(unknown)}", file=sys.stderr)
    if not files:
        print("lockage.py: no lockfile to read", file=sys.stderr)
        return 2
    at = _gh.now()
    entries = []
    for f in files:
        try:
            entries += parse(f)
        except (ValueError, KeyError, TypeError) as e:
            print(f"lockage.py: {f} does not parse: {e}", file=sys.stderr)
            return 2
    entries = classify(date_all(entries, at=at), a.min_age_days)
    young = sorted((e for e in entries if e["status"] == "young"), key=lambda e: e["age_days"])
    undated = [e for e in entries if e["status"] == "undated"]
    counts = {s: sum(e["status"] == s for e in entries) for s in ("young", "ok", "undated", "skipped")}
    if a.json:
        out = {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "min_age_days": a.min_age_days, "files": files,
               "unread_files": unknown, "counts": counts, "young": young, "undated": undated}
        if a.all:
            out["entries"] = entries
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        if young:
            print(f"Younger than {a.min_age_days:g} days:")
            for e in young:
                print(f"  {e['file']}: {e['name']} {e['version']}, published {e['published_at'][:16]}Z, "
                      f"{e['age_days']:.1f} days ago ({e['date_source']})")
        if undated:
            print("Cannot date:")
            for e in undated:
                print(f"  {e['file']}: {e['name']} {e['version']}: {e['undated']}")
        print(f"{len(entries)} entries in {len(files)} lockfiles: {counts['young']} younger than "
              f"{a.min_age_days:g} days, {counts['undated']} cannot be dated, {counts['skipped']} local or skipped.")
    return 1 if young else 0


if __name__ == "__main__":
    sys.exit(main())
