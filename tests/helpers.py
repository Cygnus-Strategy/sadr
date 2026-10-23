"""Shared test helpers: the scripts path, fixtures, and a fake `gh`."""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "plugins", "sadr", "skills", "sadr", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

# Every test uses this fixed time as "now".
NOW = datetime.datetime(2026, 10, 22, 12, 0, tzinfo=datetime.timezone.utc)


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


def fixture_json(name):
    return json.loads(fixture(name))


class FakeApi:
    """Stand-in for _gh.api. Maps a path prefix to a response.

    A response can be a value, a callable that takes the path, or an
    exception to raise. A path with no route returns None, like a 404.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, path, raw=False, paginate=False, method=None, fields=None, **kwargs):
        self.calls.append(path)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                resp = self.routes[prefix]
                if isinstance(resp, Exception):
                    raise resp
                return resp(path) if callable(resp) else resp
        return None
