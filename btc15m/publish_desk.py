"""Publish the Satoshi Desk feed to a dedicated branch of the daemon-poly repo.

Writes satoshi-desk.json via the Contents API (repo scope -- the stored
token has no gist scope). Raw URL is stable, CORS-friendly, and fetchable
without auth, so Grok's desk page can poll it:

    https://raw.githubusercontent.com/ladre777/daemon-poly/satoshi-desk-feed/satoshi-desk.json

First run creates the branch from main and stores state in
.desk_publish.json. The branch keeps feed commits off main.

Usage:
    python3 publish_desk.py          # build snapshot + publish
    python3 publish_desk.py --dry    # build snapshot, print plan only

Auth: stored custom.github credential via the skill's surrogate helper.
Raw secrets never appear here.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (  # noqa: E402
    add_surrogate_to_request,
    read_json_response,
)

import desk_feed  # noqa: E402

CREDENTIAL = "custom.github"
ALLOWED_HOSTS = ["api.github.com"]
API = "https://api.github.com"
OWNER = "ladre777"
REPO = "daemon-poly"
BRANCH = "satoshi-desk-feed"
PATH = "satoshi-desk.json"
STATE_FILE = os.path.join(HERE, ".desk_publish.json")


def _req(method: str, path: str, payload: dict | None = None):
    url = API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "muse-github-skill/1.0",
        "Content-Type": "application/json",
    })
    add_surrogate_to_request(req, CREDENTIAL, allowed_hosts=ALLOWED_HOSTS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status == 204:
            return None
        return read_json_response(resp)


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(s: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)
    os.chmod(STATE_FILE, 0o600)


def _ensure_branch() -> bool:
    """Create the feed branch from main if missing. Returns True if created."""
    try:
        _req("GET", f"/repos/{OWNER}/{REPO}/branches/{BRANCH}")
        return False
    except Exception:  # noqa: BLE001
        pass
    main = _req("GET", f"/repos/{OWNER}/{REPO}/branches/main")
    sha = main["commit"]["sha"]
    _req("POST", f"/repos/{OWNER}/{REPO}/git/refs",
         {"ref": f"refs/heads/{BRANCH}", "sha": sha})
    return True


def publish(snapshot_text: str, dry: bool = False):
    state = _load_state()
    if dry:
        print(f"dry: would publish {len(snapshot_text)} bytes to "
              f"{OWNER}/{REPO}@{BRANCH}:{PATH}")
        return None
    created = _ensure_branch()
    payload = {
        "message": "desk feed update",
        "content": base64.b64encode(snapshot_text.encode()).decode(),
        "branch": BRANCH,
    }
    if state.get("sha"):
        payload["sha"] = state["sha"]
    else:
        # state file lost but file may exist on the branch: fetch its sha
        try:
            cur = _req("GET",
                       f"/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}")
            payload["sha"] = cur["sha"]
        except Exception:  # noqa: BLE001
            pass  # file doesn't exist yet; create it
    data = _req("PUT", f"/repos/{OWNER}/{REPO}/contents/{PATH}", payload)
    _save_state({"sha": data["content"]["sha"]})
    raw_url = (f"https://raw.githubusercontent.com/{OWNER}/{REPO}/"
               f"{BRANCH}/{PATH}")
    return {"branch_created": created, "raw_url": raw_url,
            "sha": data["content"]["sha"][:7]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)
    snap = desk_feed.build_snapshot()
    text = json.dumps(snap, separators=(",", ":"))
    try:
        res = publish(text, dry=args.dry)
    except Exception as e:  # noqa: BLE001
        print(f"publish failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if res:
        print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
