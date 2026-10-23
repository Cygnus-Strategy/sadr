"""Read container image manifests and dates from an OCI registry.

Sadr dates an image digest by the `created` time in the image config. For a
multi-platform index, it reads the linux/amd64 image, or else the first
image that is not an attestation. The registry calls are anonymous, so they
work only for public images.

A registry that answers HTTP 429 (too many requests) gets up to three
more tries, after the Retry-After time (at most 60 seconds) or 5, 10 and
20 seconds. After that the client raises RateLimited, which callers report
as "not checked", not as a pin problem. Standard library only.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DOCKER_HUB = "registry-1.docker.io"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class RegistryError(RuntimeError):
    pass


class RateLimited(RegistryError):
    """The registry kept answering HTTP 429."""


RATE_RETRIES = 3
MAX_WAIT = 60
# Tests replace this to skip the waits.
sleep = time.sleep


def retry_after(headers, attempt):
    """Seconds to wait before the next try: Retry-After, capped, or 5, 10, 20."""
    value = (headers or {}).get("Retry-After") if headers is not None else None
    try:
        return min(MAX_WAIT, max(1, int(float(value))))
    except (TypeError, ValueError):
        return 5 * 2 ** attempt


def parse_ref(ref):
    """Split an image reference into {registry, repo, tag, digest, official}.

    "python:3.15-slim" is the Docker Hub image library/python. A first part
    with a dot or a colon, or "localhost", names another registry.
    """
    name, digest = ref, None
    if "@" in ref:
        name, digest = ref.split("@", 1)
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    return {"registry": registry, "repo": repo, "tag": tag, "digest": digest,
            "official": registry == "docker.io" and repo.startswith("library/")}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    """An anonymous registry client with a token cache per repo."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.tokens = {}

    def host(self, registry):
        return DOCKER_HUB if registry == "docker.io" else registry

    def _open(self, req, timeout):
        """Open REQ. Retry HTTP 429 with backoff, then raise RateLimited."""
        for attempt in range(RATE_RETRIES + 1):
            try:
                return self.opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                headers = e.headers
                e.close()
                if attempt == RATE_RETRIES:
                    raise RateLimited(f"{urllib.parse.urlsplit(req.full_url).netloc}: HTTP 429, rate limited "
                                      f"after {RATE_RETRIES + 1} tries") from None
                sleep(retry_after(headers, attempt))
        raise AssertionError("unreachable")

    def _token(self, challenge, repo):
        """Fetch an anonymous pull token for the Bearer CHALLENGE."""
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not realm or urllib.parse.urlsplit(realm).scheme != "https":
            raise RegistryError(f"unexpected auth challenge: {challenge[:120]}")
        params.setdefault("scope", f"repository:{repo}:pull")
        url = realm + "?" + urllib.parse.urlencode(params)
        with self._open(urllib.request.Request(url, headers={"User-Agent": "sadr"}), timeout=30) as f:
            data = json.loads(f.read())
        return data.get("token") or data.get("access_token")

    def get(self, registry, repo, path, accept=MANIFEST_TYPES):
        """GET /v2/REPO/PATH. Return (body bytes, headers). Follow one redirect without credentials."""
        url = f"https://{self.host(registry)}/v2/{repo}/{path}"
        return self.get_url(registry, repo, url, accept)

    def get_url(self, registry, repo, url, accept=MANIFEST_TYPES):
        """GET a URL on the registry with the repo's token. Return (body bytes, headers)."""
        path = url.split(f"/v2/{repo}/", 1)[-1]
        key = (registry, repo)
        for _ in range(2):
            headers = {"Accept": accept, "User-Agent": "sadr"}
            if self.tokens.get(key):
                headers["Authorization"] = f"Bearer {self.tokens[key]}"
            try:
                with self._open(urllib.request.Request(url, headers=headers), timeout=30) as f:
                    return f.read(), f.headers
            except urllib.error.HTTPError as e:
                e.close()
                if e.code == 401 and not self.tokens.get(key):
                    challenge = e.headers.get("WWW-Authenticate", "")
                    if not challenge.lower().startswith("bearer"):
                        raise RegistryError(f"{registry}/{repo}: HTTP 401 without a Bearer challenge") from None
                    self.tokens[key] = self._token(challenge, repo)
                    continue
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    loc = urllib.parse.urljoin(url, e.headers["Location"])
                    if urllib.parse.urlsplit(loc).scheme != "https":
                        raise RegistryError(f"refusing a non-https redirect: {loc}") from None
                    with self._open(urllib.request.Request(loc, headers={"User-Agent": "sadr"}),
                                    timeout=60) as f:
                        return f.read(), f.headers
                raise RegistryError(f"{registry}/{repo} {path}: HTTP {e.code}") from None
            except urllib.error.URLError as e:
                raise RegistryError(f"{registry}/{repo}: {e.reason}") from None
        raise RegistryError(f"{registry}/{repo}: the registry refused the anonymous token")

    def tags(self, registry, repo, limit=20000):
        """Every tag of REPO, following the registry's Link pages."""
        url = f"https://{self.host(registry)}/v2/{repo}/tags/list?n=1000"
        out = []
        while url and len(out) < limit:
            body, headers = self.get_url(registry, repo, url, accept="application/json")
            out += json.loads(body).get("tags") or []
            link = (headers or {}).get("Link") or ""
            m = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link)
            url = urllib.parse.urljoin(url, m.group(1)) if m else None
            if url and urllib.parse.urlsplit(url).scheme != "https":
                raise RegistryError(f"refusing a non-https page link: {url}")
        return out

    def manifest(self, registry, repo, ref):
        """Return (manifest dict, digest) for a tag or a digest."""
        body, headers = self.get(registry, repo, f"manifests/{ref}")
        digest = headers.get("Docker-Content-Digest") or (ref if DIGEST.fullmatch(ref) else None)
        return json.loads(body), digest

    def created(self, registry, repo, ref):
        """Return (index or image digest, config created time) for a tag or digest."""
        man, digest = self.manifest(registry, repo, ref)
        if "manifests" in man:
            pick = None
            for m in man["manifests"]:
                plat = m.get("platform") or {}
                if (m.get("annotations") or {}).get("vnd.docker.reference.type") == "attestation-manifest":
                    continue
                if plat.get("os") == "unknown":
                    continue
                if plat.get("os") == "linux" and plat.get("architecture") == "amd64":
                    pick = m
                    break
                pick = pick or m
            if not pick:
                raise RegistryError(f"{registry}/{repo}@{ref}: the index lists no image")
            man, _ = self.manifest(registry, repo, pick["digest"])
        cfg = (man.get("config") or {}).get("digest")
        if not cfg:
            raise RegistryError(f"{registry}/{repo}@{ref}: the manifest has no config")
        body, _ = self.get(registry, repo, f"blobs/{cfg}", accept="*/*")
        when = json.loads(body).get("created")
        if not when:
            raise RegistryError(f"{registry}/{repo}@{ref}: the image config has no created time")
        return digest, when
