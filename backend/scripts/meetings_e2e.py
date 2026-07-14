"""End-to-end smoke for the /meetings/* pipeline against a DEPLOYED backend.

Drives the exact desktop contract with zero desktop code: mint a Firebase ID
token for a test user, claim a meeting, upload one FLAC segment, complete,
then poll until the note is ready (Cloud Tasks -> Deepgram -> LLM -> Firestore).

Usage (from backend/):
    python scripts/meetings_e2e.py --audio path/to/sample.flac \
        [--base https://juno-backend-620715294422.us-central1.run.app] \
        [--uid meetings-e2e-test-user]

The audio file should be 2-channel 16 kHz FLAC with some speech, e.g.:
    ffmpeg -i any_voice_recording.wav -ac 2 -ar 16000 sample.flac
(A pure sine tone also exercises the pipeline; the note will just say no
speech was captured.)

Requires: service-account.json in backend/ (used to mint a custom token) and
FIREBASE_WEB_API_KEY in the environment or .env (the Identity Toolkit key used
to exchange it for an ID token).

NOTE: the claim charges the test user's monthly meeting counter like any real
user; reset users/{uid}/usage/meetings_{YYYYMM} in the Firestore console if
the cap trips during repeated runs (or use a pro-entitled test uid).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta

DEFAULT_BASE = "https://juno-backend-620715294422.us-central1.run.app"
IDENTITY_TOOLKIT = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
)


def _load_dotenv_key(name: str) -> str | None:
    if os.getenv(name):
        return os.getenv(name)
    try:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def mint_id_token(uid: str) -> str:
    import firebase_admin
    from firebase_admin import auth, credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate("service-account.json"))
    custom_token = auth.create_custom_token(uid).decode()

    api_key = _load_dotenv_key("FIREBASE_WEB_API_KEY")
    if not api_key:
        sys.exit("FIREBASE_WEB_API_KEY not set (env or .env); cannot exchange token.")

    body = json.dumps({"token": custom_token, "returnSecureToken": True}).encode()
    request = urllib.request.Request(
        f"{IDENTITY_TOOLKIT}?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["idToken"]


def _call(base: str, token: str, method: str, path: str, *,
          json_body: dict | None = None, raw_body: bytes | None = None,
          headers: dict | None = None) -> tuple[int, dict]:
    request_headers = {"Authorization": f"Bearer {token}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        request_headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        request_headers["Content-Type"] = "audio/flac"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        f"{base}{path}", data=data, method=method, headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, help="2ch 16kHz FLAC sample")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--uid", default="meetings-e2e-test-user")
    args = parser.parse_args()

    with open(args.audio, "rb") as fh:
        flac = fh.read()
    print(f"audio: {len(flac)} bytes")

    token = mint_id_token(args.uid)
    print(f"minted ID token for {args.uid}")

    now = datetime.now(UTC)
    status, body = _call(args.base, token, "POST", "/meetings/claim", json_body={
        "event_id": f"e2e-{int(now.timestamp())}",
        "title": "Meetings E2E smoke",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
        "device_id": "meetings-e2e-script",
    })
    print(f"claim -> {status} {body}")
    if status != 200:
        sys.exit("claim failed; see status above (402 = cap, reset the counter)")
    meeting_id = body["meeting_id"]

    status, body = _call(
        args.base, token, "POST", f"/meetings/{meeting_id}/segments/0",
        raw_body=flac,
        headers={"X-Segment-Start-Ms": "0",
                 "X-Segment-Duration-Ms": "300000"},
    )
    print(f"segment 0 -> {status} {body}")
    if status != 200:
        sys.exit("segment upload failed")

    status, body = _call(
        args.base, token, "POST", f"/meetings/{meeting_id}/complete",
        json_body={"segment_count": 1, "total_duration_ms": 300000,
                   "reason": "e2e"},
    )
    print(f"complete -> {status} {body}")
    if status != 200:
        sys.exit("complete failed")

    deadline = time.time() + 600
    while time.time() < deadline:
        status, body = _call(args.base, token, "GET", f"/meetings/{meeting_id}")
        state = body.get("status")
        print(f"poll -> {status} status={state}")
        if state in ("ready", "excluded", "failed"):
            print(json.dumps(body.get("note"), indent=2))
            sys.exit(0 if state == "ready" else 1)
        time.sleep(15)
    sys.exit("timed out waiting for synthesis")


if __name__ == "__main__":
    main()
