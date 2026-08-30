#!/usr/bin/env python3
"""Sync channel registry from the dedicated account's subscriptions (Keychain token)."""
import datetime, json, os, sqlite3, subprocess, sys, urllib.parse, urllib.request

HOME = os.path.expanduser("~")
DB = f"{HOME}/yt-research-pipeline/data/pipeline.db"
SECRET = f"{HOME}/Library/Application Support/yt-research-pipeline/client_secret.json"
SERVICE = "yt-research-pipeline-oauth"
ACCOUNT = "dedicated-ai-account"
TOKEN_URL = "https://oauth2.googleapis.com/token"

def keychain_get():
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("Keychain lookup failed")
    return r.stdout.strip()

def refresh_access(cid, secret, refresh):
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=60))

def list_subscriptions(access):
    subs, page = [], None
    while True:
        p = {"part": "snippet", "mine": "true", "maxResults": "50"}
        if page: p["pageToken"] = page
        req = urllib.request.Request(
            "https://www.googleapis.com/youtube/v3/subscriptions?" + urllib.parse.urlencode(p),
            headers={"Authorization": f"Bearer {access}"})
        resp = json.load(urllib.request.urlopen(req, timeout=60))
        for it in resp.get("items", []):
            sn = it["snippet"]
            subs.append({"title": sn["title"], "channel_id": sn["resourceId"]["channelId"]})
        page = resp.get("nextPageToken")
        if not page: break
    return subs

def main():
    refresh = keychain_get()
    c = json.load(open(SECRET))["installed"]
    tok = refresh_access(c["client_id"], c["client_secret"], refresh)
    subs = list_subscriptions(tok["access_token"])
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    db = sqlite3.connect(DB)
    added = updated = 0
    for s in subs:
        cur = db.execute("SELECT enabled, source FROM channels WHERE channel_id=?", (s["channel_id"],)).fetchone()
        if cur is None:
            db.execute("""INSERT INTO channels (channel_id, title, enabled, priority, source, last_sync_at)
                          VALUES (?,?,1,0,'dedicated-sync',?)""", (s["channel_id"], s["title"], now))
            added += 1
        else:
            db.execute("""UPDATE channels SET title=?, enabled=1, source='dedicated-sync', last_sync_at=?
                          WHERE channel_id=?""", (s["title"], now, s["channel_id"]))
            updated += 1
    # disable channels no longer subscribed (never delete — audit history)
    subscribed_ids = {s["channel_id"] for s in subs}
    for (cid,) in db.execute("SELECT channel_id FROM channels WHERE enabled=1").fetchall():
        if cid not in subscribed_ids:
            db.execute("UPDATE channels SET enabled=0 WHERE channel_id=?", (cid,))
            print(f"disabled (unsubscribed): {cid}")
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM channels WHERE enabled=1").fetchone()[0]
    print(f"sync complete: {len(subs)} subscriptions | +{added} new, ={updated} updated, enabled total={n}")
    db.close()

if __name__ == "__main__":
    main()
