#!/usr/bin/env python3
"""Resolve a published stable release or explicit ref to an immutable SHA."""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://api.github.com/repos/NousResearch/hermes-agent"


def read(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hls-native-compat"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(API + path, headers=headers), timeout=30) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="")
    args = parser.parse_args()
    ref = args.ref
    if not ref:
        release = read("/releases/latest")
        ref = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d{1,3}\.\d+\.\d+", ref):
            raise SystemExit("No published final release")
    commit = read("/commits/" + quote(ref, safe=""))
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit("Cannot resolve immutable host commit")
    print(json.dumps({"revision": sha, "ref": ref}))
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a") as stream:
            stream.write(f"revision={sha}\n")


if __name__ == "__main__":
    main()
