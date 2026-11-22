#!/usr/bin/env python3
"""Append a fingerprint from an official, already published commit."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "source-fingerprints.json"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("published_commit", help="Full commit SHA verified on the official GitHub repository")
    parser.add_argument("--tag", help="Optional published tag pointing to this commit")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.published_commit):
        parser.error("provide the full published commit SHA")
    if args.tag and git("rev-parse", "--verify", f"refs/tags/{args.tag}^{{commit}}").decode().strip() != args.published_commit:
        parser.error("tag does not point to the published commit")

    archive = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    source = git("show", f"{args.published_commit}:{archive['source']}").decode("utf-8")
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(r'^    plugin_version\s*=\s*[\"\']([^\"\']+)', source, re.M)
    if not match:
        parser.error("plugin_version is missing from the published source")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    releases = archive["releases"]
    entry = next((r for r in releases if r["source_sha256"] == digest), None)
    if entry is None:
        entry = {
            "version": match[1], "source_sha256": digest, "commit": args.published_commit,
            "reports_fingerprint": bool(re.search(r'^\s+[\"\']source_sha256[\"\']\s*:\s*_source_fingerprint\(', source, re.M)),
            "tags": [],
        }
        releases.append(entry)
    if args.tag and args.tag not in entry["tags"]:
        if any(args.tag in record["tags"] for record in releases):
            parser.error("tag already identifies a different archived source")
        entry["tags"].append(args.tag)
    ARCHIVE.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")
    print(f"{entry['version']} {digest}")


if __name__ == "__main__":
    main()
