#!/usr/bin/env python3
"""OAuth bootstrap for the YouTube research pipeline.

One-time consent flow:
1. Starts a local loopback HTTP server on 127.0.0.1:8765
2. Opens the system browser for Google consent (PKCE + offline access)
3. Exchanges the authorization code
4. Stores the refresh token in macOS Keychain (never on disk, never in logs)
5. Verifies with a live subscriptions.list call and prints channel count + list
"""
import base64, hashlib, http.server, json, os, secrets, subprocess, sys, threading, urllib.parse, urllib.request

HOME = os.path.expanduser("~")
SECRET_PATH = f"{HOME}/Library/Application Support/yt-research-pipeline/client_secret.json"
KEYCHAIN_SERVICE = "yt-research-pipeline-oauth"
KEYCHAIN_ACCOUNT = "dedicated-ai-account"
REDIRECT = "http://localhost:8765"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

def load_client():
    d = json.load(open(SECRET_PATH))
    c = d["installed"]
    return c["client_id"], c["client_secret"]

def keychain_set(secret: str):
    subprocess.run([
        "security", "add-generic-password", "-U",
        "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
        "-w", secret,
    ], check=True, capture_output=True)

def keychain_get():
    r = subprocess.run([
        "security", "find-generic-password",
        "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
    ], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None

def main():
    client_id, client_secret = load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            result["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Authorization complete. You can close this tab and return to the terminal." if result.get("code") else f"Authorization failed: {result.get('error')}"
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
        def log_message(self, *a):  # silence request logging (could echo tokens in URL)
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("\nOpening browser for one-time Google consent...")
    print("Scope requested: youtube.readonly ONLY\n")
    subprocess.run(["open", url])

    # Wait for the callback (up to 10 minutes)
    import time
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(1)
        if result.get("code") or result.get("error"):
            break
    server.server_close()

    if result.get("error"):
        print(f"FAILED: {result['error']}"); sys.exit(1)
    if not result.get("code"):
        print("FAILED: no code received (timeout). Consent URL was:", url); sys.exit(1)
    if result.get("state") != state:
        print("FAILED: state mismatch (possible CSRF)"); sys.exit(1)

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }).encode()
    resp = json.load(urllib.request.urlopen(urllib.request.Request(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=60))
    refresh = resp.get("refresh_token")
    if not refresh:
        print("FAILED: no refresh_token in response"); sys.exit(1)

    keychain_set(refresh)
    print("✓ Refresh token stored in macOS Keychain (service=%s)" % KEYCHAIN_SERVICE)

    # Verify: refresh -> access token -> subscriptions.list
    access = refresh_access(client_id, client_secret, refresh)
    subs = list_subscriptions(access)
    print(f"\n✓ subscriptions.list OK — {len(subs)} subscriptions found:\n")
    for s in subs:
        print(f"  {s['title']}  [{s['channel_id']}]")

def refresh_access(client_id, client_secret, refresh):
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    resp = json.load(urllib.request.urlopen(urllib.request.Request(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=60))
    return resp["access_token"]

def list_subscriptions(access_token):
    subs, page_token = [], None
    while True:
        params = {"part": "snippet", "mine": "true", "maxResults": "50"}
        if page_token: params["pageToken"] = page_token
        url = "https://www.googleapis.com/youtube/v3/subscriptions?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        resp = json.load(urllib.request.urlopen(req, timeout=60))
        for item in resp.get("items", []):
            sn = item["snippet"]
            subs.append({"title": sn["title"], "channel_id": sn["resourceId"]["channelId"]})
        page_token = resp.get("nextPageToken")
        if not page_token: break
    return subs

if __name__ == "__main__":
    main()
