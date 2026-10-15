#!/usr/bin/env python3
"""Answer a comment in a page's thread — the user reads it right under their question.

    reply.py <page_dir> <comment_id> "answer" [--suggest]

--suggest marks the answer as implying a change: the page shows "Make it a change request" so the
user decides; never revise the page just because a conversation led there.

Writes through the page's server (only the server writes state.json): a multi-page server if the page
sits under one (<dir>/.appserver.port for the App Maker, <dir>/.pages.port for others such as Ideation),
else the page's own server (<page_dir>/.port).
Short and plain; **bold**, `code`, line breaks and "- " bullets render.
"""
import json, os, sys, urllib.request

args = [a for a in sys.argv[1:] if a != "--suggest"]
if len(args) < 3:
    raise SystemExit(__doc__)
page, cid, text = os.path.abspath(args[0]), args[1], " ".join(args[2:])
url = None
d = page
while d != os.path.dirname(d):                      # walk up to an app with an app server
    # a server for many pages: the App Maker's app server, or any other (e.g. Ideation) via .pages.port
    pf = next((os.path.join(d, f) for f in (".appserver.port", ".pages.port") if os.path.exists(os.path.join(d, f))), None)
    if pf:
        rel = os.path.relpath(page, d).replace(os.sep, "/")
        url = f"http://127.0.0.1:{open(pf).read().strip()}/{'' if rel == '.' else rel + '/'}api/answer"
        break
    d = os.path.dirname(d)
if not url and os.path.exists(os.path.join(page, ".port")):
    url = f"http://127.0.0.1:{open(os.path.join(page, '.port')).read().strip()}/api/answer"
if not url:
    raise SystemExit("no server for this page — start the App Maker or the page's server")
req = urllib.request.Request(url, data=json.dumps({"id": cid, "text": text, "suggest": "--suggest" in sys.argv}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
r = json.loads(urllib.request.urlopen(req, timeout=15).read())
print("answered" if r.get("ok") else r)
