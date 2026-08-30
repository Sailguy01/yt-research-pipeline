#!/usr/bin/env python3
"""Verify the stored OAuth token: refresh -> subscriptions.list, with verbose API error output."""
import json, os, subprocess, sys, urllib.error, urllib.parse, urllib.request

HOME = os.path.expanduser("~")
KEYCHAIN_SERVICE = "yt-research-pipeline-oauth"
KEYCHAIN_ACCOUNT = "dedicated-ai-account"
TOKEN_URL = "https://oauth2.googleapis.com/token"

def keychain_get():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                        "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, text=True)
    if r.returncode != 0:
        print("Keychain lookup FAILED:", r.stderr.strip()); sys.exit(1)
    return r.stdout.strip()

def refresh_access(client_id, client_secret, refresh):
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        print("refresh HTTP error:", e.code, e.read().decode()[:500]); raise SystemExit(1)

def api_call(url, access):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"API HTTP {e.code}: {body[:1500]}")
        # tokeninfo to see granted scopes
        ti = json.load(urllib.request.urlopen(
            "https://oauth2.googleapis.com/tokeninfo?access_token=" + access, timeout=30))
        print("tokeninfo scopes:", ti.get("scope"), "| email:", ti.get("email"), "| expires_in:", ti.get("expires_in"))
        raise SystemExit(1)

def main():
    refresh = keychain_get()
    d = json.load(open(f"{HOME}/Library/Application Support/yt-research-pipeline/client_secret.json"))
    c = d["installed"]
    tok = refresh_access(c["client_id"], c["client_secret"], refresh)
    access = tok["access_token"]
    print("refresh OK; scope granted:", tok.get("scope"))

    ti = json.load(urllib.request.urlopen(
        "https://oauth2.googleapis.com/tokeninfo?access_token=" + access, timeout=30))
    print("tokeninfo: email=%s scopes=%s" % (ti.get("email"), ti.get("scope")))

    params = {"part": "snippet", "mine": "true", "maxResults": "50"}
    url = "https://www.googleapis.com/youtube/v3/subscriptions?" + urllib.parse.urlencode(params)
    resp = api_call(url, access)
    items = resp.get("items", [])
    print(f"\n✓ subscriptions.list OK — page totalItems={resp.get('pageInfo',{}).get('totalItems')}, this page={len(items)}\n")
    for it in items:
        sn = it["snippet"]
        print(f"  {sn['title']}  [{sn['resourceId']['channelId']}]")
    if resp.get("nextPageToken"):
        print("  ... (more pages)")

if __name__ == "__main__":
    main()
