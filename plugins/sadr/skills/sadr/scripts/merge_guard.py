#!/usr/bin/env python3
"""Check whether Sadr may turn on auto-merge for one PR kind in one repo.

Sadr turns on auto-merge only when GitHub itself guards the merge:

- the repo allows auto-merge (the "Allow auto-merge" setting)
- the default branch requires at least one status check, through branch
  protection or a ruleset

The script prints JSON with allow_auto_merge, the allowed merge methods,
required_checks (the number of required checks, and their names), and
eligible. When GitHub does not let the token read the protection rules,
required_checks is null, cannot_verify gives the reason, and eligible is
false. Branch protection and rulesets answer HTTP 403 on a private repo
of a free plan. They answer HTTP 404 when the token cannot read them.

A run opens three kinds of PR, and each has its own setting, off by
default: facts (on or off), upkeep (off, patch, minor or all) and issue
(on or off). --kind names the PR's kind. The setting comes from
--setting, or else from --settings-text, given exactly twice and in this
order: first the owner's text (owner_sadr_section in repos.py), then the
repo's (sadr_section). Pass "" for a text that is null. Each text can be
a whole ## Sadr section: when it has a ### Settings subsection, only that
subsection counts. Settings are plain sentences, for example "Auto-merge
facts and housekeeping PRs. Enhancement PRs need review." A later
sentence wins, so a repo sentence overrides the owner's for the kinds it
names and leaves the other kinds alone. setting_source in the output
says where the kind's setting came from: repo, owner, or default (Sadr's
default, off). With some other number of texts, it names the text by its
position. A sentence about auto-merge that the script cannot read is
listed under unclear, and changes nothing.

For an upkeep PR, pass each version change with --bump NAME=FROM..TO.
The script classifies it as patch, minor or major. Before 1.0, a change
of the first non-zero number is major: 0.143 to 0.148 is major, and so
is 0.0.3 to 0.0.4. A NAME that starts with "runtime:" (such as
runtime:node) counts a new minor line as major: Python 3.14 to 3.15 is
a new line. A version that the script cannot read, a rollback or a
prerelease counts as major. The PR is eligible only when every bump is
within the upkeep level.

When the setting is off for the kind, the script makes no API call:
eligible is false, and Sadr adds no "Needs you" item for the guards.
Account-wide gaps never block auto-merge, and the script does not see
them.

Examples:
  merge_guard.py --repo acme/app
  merge_guard.py --repo acme/app --kind facts --setting on
  merge_guard.py --repo acme/app --kind upkeep --setting minor \\
      --bump vite=7.1.0..7.2.1 --bump runtime:node=24.11.0..24.11.1
  merge_guard.py --repo acme/app --kind issue \\
      --settings-text "Auto-merge everything." --settings-text "Enhancement PRs need review."
  merge_guard.py --repo acme/app --kind facts --settings-text "" --settings-text "$REPO_SECTION"
  merge_guard.py classify 0.143.0 0.148.0

Needs an authenticated `gh` CLI. Standard library only.
"""
import argparse
import json
import re
import sys

import _gh


def get(path):
    """Return (data, None) for a GitHub API path, or (None, GhError)."""
    try:
        return json.loads(_gh.run(["api", path]) or "null"), None
    except _gh.GhError as e:
        return None, e


def reason_of(err):
    """The message of a GhError without the command: "Branch not protected (HTTP 404)"."""
    text = str(err).split(": ", 1)[-1] if ": " in str(err) else str(err)
    text = text[len("gh: "):] if text.startswith("gh: ") else text
    return f"{text} (HTTP {err.status})" if err.status and f"HTTP {err.status}" not in text else text


def classic_checks(repo, branch):
    """(check names, None) from branch protection, or (None, reason)."""
    data, err = get(f"repos/{repo}/branches/{branch}/protection/required_status_checks")
    if err is None:
        names = set(data.get("contexts") or [])
        names |= {c.get("context") for c in data.get("checks") or [] if c.get("context")}
        return names, None
    if err.status == 404 and "Branch not protected" in str(err):
        return set(), None
    if err.status == 404 and "Required status checks not enabled" in str(err):
        return set(), None
    return None, f"branch protection: {reason_of(err)}"


def ruleset_checks(repo, branch):
    """(check names, None) from the rulesets that apply to BRANCH, or (None, reason)."""
    data, err = get(f"repos/{repo}/rules/branches/{branch}")
    if err is not None:
        return None, f"rulesets: {reason_of(err)}"
    names = set()
    for rule in data or []:
        if rule.get("type") == "required_status_checks":
            for c in (rule.get("parameters") or {}).get("required_status_checks") or []:
                if c.get("context"):
                    names.add(c["context"])
    return names, None


def guard(repo):
    """Return the merge guard facts of REPO as a dict."""
    info, err = get(f"repos/{repo}")
    if err is not None:
        return {"repo": repo, "allow_auto_merge": None, "merge_methods": [], "required_checks": None,
                "required_check_names": [], "cannot_verify": f"repo: {reason_of(err)}", "eligible": False,
                "why_not": [f"GitHub did not return the repo: {reason_of(err)}"]}
    branch = info.get("default_branch") or "main"
    methods = [m for m, key in (("merge", "allow_merge_commit"), ("squash", "allow_squash_merge"),
                                ("rebase", "allow_rebase_merge")) if info.get(key)]
    classic, why_classic = classic_checks(repo, branch)
    rules, why_rules = ruleset_checks(repo, branch)
    problems = [w for w in (why_classic, why_rules) if w]
    cannot = "; ".join(problems) if problems else None
    names = sorted((classic or set()) | (rules or set()))
    required = None if cannot else len(names)
    auto = bool(info.get("allow_auto_merge"))
    why_not = []
    if not auto:
        why_not.append("the repo does not allow auto-merge")
    if cannot:
        why_not.append(f"Sadr cannot verify the required checks ({cannot})")
    elif not required:
        why_not.append(f"{branch} requires no status check")
    if not methods:
        why_not.append("the repo allows no merge method")
    return {"repo": repo, "default_branch": branch, "private": info.get("private"), "allow_auto_merge": auto,
            "merge_methods": methods, "required_checks": required, "required_check_names": names,
            "cannot_verify": cannot, "eligible": not why_not, "why_not": why_not}


KINDS = ("facts", "upkeep", "issue")
UPKEEP_LEVELS = ("off", "patch", "minor", "all")
ORDER = {"none": 0, "patch": 1, "minor": 2, "major": 3}
ALLOWS = {"off": -1, "patch": 1, "minor": 2, "all": 3}
VERSION = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?")
AUTO_MERGE = re.compile(r"auto[- ]?merg", re.I)
NEGATIVE = re.compile(r"(don'?t|do not|never|no|not)\s+(?:\w+\s+){0,2}auto[- ]?merg|need(s)?\s+(a\s+)?(human\s+)?review|"
                      r"require(s)?\s+(a\s+)?review|auto[- ]?merge\s*(?::|is|=)?\s*off\b|maintainers?\s+merges?", re.I)
KIND_WORDS = {
    "facts": re.compile(r"\bfacts?\b|agents\.md", re.I),
    "upkeep": re.compile(r"housekeeping|upkeep|dependenc|\bupdates?\b|upgrades?|\bbumps?\b", re.I),
    "issue": re.compile(r"enhancement|feature|\bissues?\b", re.I),
}
EVERYTHING = re.compile(r"\b(everything|all\s+(sadr\s+)?prs|every\s+(sadr\s+)?pr|all\s+of\s+them)\b", re.I)


def parse_version(v):
    """'v1.2.3' -> (1, 2, 3). None for a prerelease or anything that is not numeric."""
    m = VERSION.fullmatch((v or "").strip())
    if not m:
        return None
    nums = [int(x) for x in m.groups() if x is not None]
    return tuple(nums + [0] * (3 - len(nums)))


def bump_level(old, new, runtime=False):
    """Classify a version change as none, patch, minor or major.

    Before 1.0 the first non-zero number is the major one. With RUNTIME, a
    change of the minor number (a new line, such as Python 3.14 to 3.15)
    is major. A rollback or a version that does not parse is major.
    """
    a, b = parse_version(old), parse_version(new)
    if a is None or b is None or b < a:
        return "major"
    if a == b:
        return "none"
    if a[0] != b[0]:
        return "major"
    if a[0] == 0:
        if a[1] != b[1]:
            return "major"
        if a[1] == 0:
            return "major" if a[2] != b[2] else "patch"
        return "patch"
    if a[1] != b[1]:
        return "major" if runtime else "minor"
    return "patch"


def parse_bump(spec):
    """'vite=7.1.0..7.2.1' -> dict with name, from, to, runtime and level."""
    name, sep, rest = spec.partition("=")
    old, sep2, new = rest.partition("..")
    if not (sep and sep2 and name and old and new):
        raise ValueError(f"--bump takes NAME=FROM..TO, not {spec!r}")
    runtime = name.startswith("runtime:")
    return {"name": name, "from": old, "to": new, "runtime": runtime,
            "level": bump_level(old, new, runtime)}


def sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.;!])\s+|\n+", text or "") if x.strip()]


def settings_part(text):
    """The ### Settings subsection of TEXT, or all of TEXT when it has none.

    The subsection ends at the next heading of level one to three. Lines in
    code fences do not count as headings.
    """
    lines, inside, found, fence = [], False, False, False
    for line in (text or "").splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
        if not fence and re.match(r"#{1,3}(\s|$)", line):
            inside = re.fullmatch(r"###\s+Settings\s*#*\s*", line, re.I) is not None
            found = found or inside
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines) if found else (text or "")


def source_names(texts):
    """The source label of each Settings text: owner and repo for two texts."""
    if len(texts) == 2:
        return ["owner", "repo"]
    return [f"settings text {i + 1}" for i in range(len(texts))]


def parse_settings(texts):
    """Resolve the per-kind auto-merge settings from plain-language sentences.

    TEXTS are the owner's Settings text, then the repo's. Return
    (settings, unclear): settings maps facts and issue to on or off, and
    upkeep to off, patch, minor or all; unclear lists the sentences about
    auto-merge that the script could not read.
    """
    settings, unclear, _ = resolve_settings(texts)
    return settings, unclear


def resolve_settings(texts):
    """parse_settings(), plus the source of each kind's setting.

    Return (settings, unclear, sources). sources maps each kind to the
    label of the text whose sentence set it last (see source_names), or
    to default when no sentence named it.
    """
    settings = {"facts": "off", "upkeep": "off", "issue": "off"}
    sources = dict.fromkeys(KINDS, "default")
    unclear = []
    for text, name in zip(texts, source_names(texts)):
        for sent in sentences(settings_part(text)):
            low = sent.lower().strip("-*• ")
            neg = NEGATIVE.search(low)
            if not (AUTO_MERGE.search(low) or neg):
                continue
            kinds = [k for k, rx in KIND_WORDS.items() if rx.search(low)]
            level = ("patch" if re.search(r"\bpatch", low) else "minor" if re.search(r"\bminor", low)
                     else "all" if re.search(r"\bmajor|\ball\b|\bany\b|\bevery", low) else None)
            if neg:
                for k in kinds or KINDS:
                    settings[k], sources[k] = "off", name
                continue
            if EVERYTHING.search(low) and not kinds:
                settings.update(facts="on", upkeep="all", issue="on")
                sources.update(dict.fromkeys(KINDS, name))
                continue
            if not kinds and level in ("patch", "minor"):
                kinds = ["upkeep"]
            if not kinds:
                unclear.append(sent)
                continue
            for k in kinds:
                settings[k] = (level or "all") if k == "upkeep" else "on"
                sources[k] = name
    return settings, unclear, sources


def normalize_setting(kind, value):
    """Map a --setting value to on/off (facts, issue) or a level (upkeep)."""
    v = (value or "off").strip().lower()
    if kind == "upkeep":
        v = {"on": "all", "true": "all", "false": "off", "everything": "all"}.get(v, v)
        if v not in UPKEEP_LEVELS:
            raise ValueError(f"--setting for upkeep is one of {', '.join(UPKEEP_LEVELS)}")
        return v
    if v in ("on", "true", "all", "yes"):
        return "on"
    if v in ("off", "false", "no"):
        return "off"
    raise ValueError(f"--setting for {kind} is on or off")


def decide(repo, kind, setting, bumps=(), unclear=(), guard_fn=None, source=None):
    """The guard facts of REPO plus whether auto-merge is allowed for a PR of KIND."""
    enabled = setting not in ("off", None)
    base = {"kind": kind, "setting": setting, "setting_source": source, "settings_unclear": list(unclear),
            "bumps": list(bumps)}
    if not enabled:
        return {"repo": repo, **base, "guard_checked": False, "eligible": False,
                "why_not": [f"auto-merge is off for {kind} PRs"]}
    res = (guard_fn or guard)(repo)
    why_not = list(res.get("why_not") or [])
    if kind == "upkeep":
        allowed = ALLOWS[setting]
        for b in bumps:
            if ORDER[b["level"]] > allowed:
                why_not.append(f"{b['name']} {b['from']} to {b['to']} is a {b['level']} change, "
                               f"above the upkeep setting {setting}")
    return {**res, **base, "guard_checked": True, "eligible": not why_not, "why_not": why_not}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["classify"]:
        cp = argparse.ArgumentParser(prog="merge_guard.py classify",
                                     description="classify one version change as none, patch, minor or major")
        cp.add_argument("old")
        cp.add_argument("new")
        cp.add_argument("--runtime", action="store_true", help="a runtime or toolchain: a new minor line is major")
        c = cp.parse_args(argv[1:])
        print(bump_level(c.old, c.new, c.runtime))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument("--kind", choices=KINDS, help="the PR kind: facts, upkeep or issue")
    ap.add_argument("--setting", help="the resolved setting for the kind: on/off (facts, issue) or "
                                      "off/patch/minor/all (upkeep)")
    ap.add_argument("--settings-text", action="append", default=[], metavar="TEXT",
                    help="plain-language Settings text, given twice: the owner's (owner_sadr_section) first, "
                         "then the repo's (sadr_section). Pass \"\" for a null text")
    ap.add_argument("--bump", action="append", default=[], metavar="NAME=FROM..TO",
                    help="a version change in an upkeep PR (repeat). Prefix NAME with runtime: for a runtime")
    ap.add_argument("--json", action="store_true", help="print JSON (the default, kept for the shared interface)")
    a = ap.parse_args(argv)
    if "/" not in a.repo:
        ap.error("--repo takes OWNER/NAME")
    if (a.setting or a.settings_text or a.bump) and not a.kind:
        ap.error("--setting, --settings-text and --bump go with --kind")
    if not a.kind:
        json.dump(guard(a.repo), sys.stdout, indent=2)
        print()
        return 0
    try:
        bumps = [parse_bump(b) for b in a.bump]
        unclear = []
        if a.setting is not None:
            setting, source = normalize_setting(a.kind, a.setting), "--setting"
        else:
            settings, unclear, sources = resolve_settings(a.settings_text)
            setting, source = settings[a.kind], sources[a.kind]
    except ValueError as e:
        ap.error(str(e))
    json.dump(decide(a.repo, a.kind, setting, bumps, unclear, source=source), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
