"""Shared helpers for Sadr's scripts: calls to the GitHub CLI, dates, and --json.

Every script reaches GitHub only through `gh`, so it uses the caller's
`gh auth` login or the GH_TOKEN in the environment. Standard library only.

GitHub sometimes answers with a server error or with an empty page for a
list that is not empty. `run` retries server errors and dropped
connections. `api` checks an empty paginated list once more without
pagination. When the two answers disagree after the retries, `api` raises
SuspectEmpty, and the script exits with a nonzero code.
"""
import argparse
import datetime
import json
import re
import subprocess
import time

RETRIES = 4
TRANSIENT = re.compile(
    r"HTTP 5\d\d|\b50[0-4]\b|connection reset|connection refused|timed? ?out|unexpected EOF|"
    r"TLS handshake|i/o timeout|secondary rate limit|something went wrong",
    re.I)
# Tests replace this to skip the waits.
sleep = time.sleep


class GhError(RuntimeError):
    """A `gh` command failed."""

    @property
    def status(self):
        """The HTTP status in the error text, or None."""
        m = re.search(r"HTTP (\d{3})", str(self))
        return int(m.group(1)) if m else None


class SuspectEmpty(GhError):
    """GitHub returned an empty list that a second call contradicts."""


def run(args, retries=RETRIES):
    """Run `gh` with ARGS. Return stdout, or raise GhError on failure.

    Retry a server error (HTTP 5xx) or a dropped connection up to RETRIES
    times, and wait 2, 4, 8 and 16 seconds between the tries.
    """
    for attempt in range(retries + 1):
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        err = r.stderr.strip() or "exit " + str(r.returncode)
        if attempt < retries and TRANSIENT.search(err):
            sleep(2 ** (attempt + 1))
            continue
        raise GhError(f"gh {' '.join(args)}: {err}")
    raise AssertionError("unreachable")


def _flatten(data):
    flat = []
    for page in data or []:
        flat.extend(page if isinstance(page, list) else [page])
    return flat


def api(path, raw=False, paginate=False, method=None, fields=None, confirm_empty=True):
    """Call `gh api PATH`. Return parsed JSON, or text when RAW is true.

    Return None when GitHub answers 404, so a missing file or tag is not an
    error. When a paginated list comes back empty, call the first page again
    without pagination. If that page is not empty, retry the paginated call
    twice, then raise SuspectEmpty.
    """
    args = ["api"]
    if method:
        args += ["-X", method]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw"]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]

    def call(extra):
        try:
            return run(args + extra + [path])
        except GhError as e:
            if e.status == 404 or "Not Found" in str(e):
                return None
            raise

    if not paginate:
        out = call([])
        if out is None or raw:
            return out
        return json.loads(out or "null")
    for attempt in range(3):
        out = call(["--paginate", "--slurp"])
        if out is None:
            return None
        # --slurp gives one list entry per page. Flatten the pages.
        flat = _flatten(json.loads(out or "null"))
        if flat or not confirm_empty or method not in (None, "GET"):
            return flat
        single = call([])
        first = json.loads(single or "null") if single else None
        if not first:
            return flat
        sleep(2 ** (attempt + 1))
    raise SuspectEmpty(f"gh api {path}: the paginated list was empty three times, "
                       f"but the first page alone has {len(first) if isinstance(first, list) else 'some'} items")


def graphql(query, **variables):
    """Run a GraphQL query. Return its `data` object."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        flag = "-F" if isinstance(v, (int, bool)) else "-f"
        args += [flag, f"{k}={str(v).lower() if isinstance(v, bool) else v}"]
    data = json.loads(run(args) or "{}")
    if data.get("errors"):
        raise GhError(f"GraphQL: {data['errors'][0].get('message')}")
    return data.get("data") or {}


def tag_date(repo, tag):
    """Return (commit SHA, date, source) for a tag of a GitHub repo.

    The date is the publish time of the tag's release, or else the tagger
    date of an annotated tag. A lightweight tag without a release has no
    date that Sadr trusts: the commit date is set by whoever made the
    commit, so the date is None and the source says why. Return
    (None, None, reason) for a tag that does not exist.
    """
    ref = api(f"repos/{repo}/git/ref/tags/{tag}")
    if not ref or not isinstance(ref, dict):
        return None, None, f"tag {tag} not found in {repo}"
    obj = ref.get("object") or {}
    sha, tagger = obj.get("sha"), None
    if obj.get("type") == "tag":
        tag_obj = api(f"repos/{repo}/git/tags/{obj['sha']}") or {}
        tagger = (tag_obj.get("tagger") or {}).get("date")
        sha = (tag_obj.get("object") or {}).get("sha") or sha
    rel = api(f"repos/{repo}/releases/tags/{tag}") or {}
    if rel.get("published_at") and not rel.get("draft"):
        return sha, rel["published_at"], "release"
    if tagger:
        return sha, tagger, "annotated tag"
    return sha, None, "cannot date: no release and a lightweight tag (Sadr never uses the commit date)"


def parse_time(ts):
    """Parse an ISO 8601 time into an aware datetime. A naive time is UTC."""
    ts = ts.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    # Python 3.10 parses at most 6 digits of fractional seconds.
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    t = datetime.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def age_days(ts, at=None):
    """Days between the ISO time TS and AT (default: now)."""
    return ((at or now()) - parse_time(ts)).total_seconds() / 86400


def semver(tag):
    """'v1.2.3' -> (1, 2, 3). Return None for anything that is not X.Y.Z."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def add_json(parser, sub=False):
    """Add --json to PARSER. A subcommand's copy keeps a --json given before it."""
    parser.add_argument("--json", action="store_true", help="print JSON",
                        **({"default": argparse.SUPPRESS} if sub else {}))
