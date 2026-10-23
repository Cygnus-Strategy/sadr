#!/usr/bin/env python3
"""Check the release criteria for the Sadr App setup.

  check-release.py [REPO_ROOT]

- app/manifest.json is valid JSON, asks for exactly the permissions in
  references/permissions.md, has its webhook off, is public and subscribes
  to no events.
- app/create.html embeds the same manifest.
- assets/app/sadr-app-logo.png is a 512x512 PNG under 1 MB.
- README.md has the collapsed manual setup and the agent prompt.

Exits 1 and prints each problem when a check fails.
"""
import json
import re
import struct
import sys
from pathlib import Path

ACCESS = {"read and write": "write", "read": "read"}
NAMES = {
    "Contents": "contents",
    "Pull requests": "pull_requests",
    "Issues": "issues",
    "Workflows": "workflows",
    "Checks": "checks",
    "Commit statuses": "statuses",
    "Metadata": "metadata",
}


def documented_permissions(text):
    """The permission table under "## The App's permissions" in permissions.md."""
    section = text.split("## The App's permissions", 1)[1].split("\n## ", 1)[0]
    perms = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in NAMES and cells[1] in ACCESS:
            perms[NAMES[cells[0]]] = ACCESS[cells[1]]
    return perms


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def main(root):
    problems = []
    try:
        manifest = json.loads((root / "app/manifest.json").read_text())
    except (OSError, ValueError) as e:
        print(f"app/manifest.json: {e}")
        return 1

    doc = documented_permissions((root / "plugins/sadr/skills/sadr/references/permissions.md").read_text())
    if len(doc) != len(NAMES):
        problems.append(f"permissions.md: expected {len(NAMES)} permissions in the table, found {len(doc)}")
    if manifest.get("default_permissions") != doc:
        problems.append(f"app/manifest.json: default_permissions {manifest.get('default_permissions')} != permissions.md {doc}")
    if manifest.get("name") != "Sadr for OWNER":
        problems.append("app/manifest.json: name is not the 'Sadr for OWNER' template")
    if manifest.get("url") != "https://sadr.cygnusstrategy.com":
        problems.append("app/manifest.json: url is not https://sadr.cygnusstrategy.com")
    if (manifest.get("hook_attributes") or {}).get("active") is not False:
        problems.append("app/manifest.json: hook_attributes.active is not false")
    if manifest.get("public") is not True:
        problems.append("app/manifest.json: public is not true")
    if manifest.get("default_events"):
        problems.append("app/manifest.json: default_events is not empty")

    html = (root / "app/create.html").read_text()
    m = re.search(r'<script type="application/json" id="manifest">(.*?)</script>', html, re.S)
    if not m or json.loads(m.group(1)) != manifest:
        problems.append("app/create.html: the embedded manifest differs from app/manifest.json")

    logo = root / "assets/app/sadr-app-logo.png"
    if not logo.is_file():
        problems.append(f"{logo.relative_to(root)}: missing")
    else:
        data = logo.read_bytes()
        if png_size(data) != (512, 512):
            problems.append(f"{logo.relative_to(root)}: not a 512x512 PNG ({png_size(data)})")
        if len(data) >= 1024 * 1024:
            problems.append(f"{logo.relative_to(root)}: {len(data)} bytes, not under 1 MB")

    readme = (root / "README.md").read_text()
    details = re.search(r"<details>\s*<summary>Manual setup of the Sadr App</summary>(.*?)</details>", readme, re.S)
    if not details or "app/README.md" not in details.group(1):
        problems.append("README.md: no <details> block 'Manual setup of the Sadr App' that links app/README.md")
    prompt = re.search(r"### Agent instructions\s.*?```text\n(.*?)```", readme, re.S)
    if not prompt:
        problems.append("README.md: no '### Agent instructions' section with a ```text prompt")
    else:
        for word in ("create.html", "SADR_APP_ID", "SADR_APP_PRIVATE_KEY", "sadr-app-logo.png", "Install the App"):
            if word not in prompt.group(1):
                problems.append(f"README.md: the agent prompt does not mention {word}")
        if "Claude Code" in prompt.group(1):
            problems.append("README.md: the agent prompt names Claude Code. Keep it agent-neutral.")

    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
