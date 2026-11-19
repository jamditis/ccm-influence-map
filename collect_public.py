"""Collect bounded, public social-network observations from an explicit roster."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from netmap import Observation, key_for


APPVIEW = "https://public.api.bsky.app/xrpc/"
HOSTS = {
    "LinkedIn": {"linkedin.com", "www.linkedin.com"},
    "Threads": {"threads.com", "www.threads.com", "threads.net", "www.threads.net"},
    "X": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    "Instagram": {"instagram.com", "www.instagram.com"},
}
WINDOW = 90
POSTS = 12


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def put(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    draft = path.with_suffix(".tmp")
    draft.write_text(json.dumps(contents, ensure_ascii=False), encoding="utf-8")
    draft.replace(path)


def bsky_api(method, **params):
    sleep(0.2)
    with urlopen(APPVIEW + method + "?" + urlencode(params), timeout=20) as response:
        return json.load(response)


def candidate_profiles(staff):
    for person in staff:
        names = person.get("alternate_names", []) + [person["name"]]
        for name in names:
            response = bsky_api("app.bsky.actor.searchActors", q=name, limit=12)
            for profile in response["actors"]:
                if profile.get("displayName", "").lower() == name.lower():
                    print(json.dumps({
                        "staff": person["id"], "handle": profile["handle"], "did": profile["did"],
                        "description": (profile.get("description") or "")[:350],
                    }, ensure_ascii=False))


def bsky_rows(person, handle, cutoff, api=bsky_api):
    profile = api("app.bsky.actor.getProfile", actor=handle)
    did = profile["did"]
    rows = []
    coverage = []

    def add(actor, relation, event):
        if actor["did"] != did:
            rows.append(Observation(person["id"], "Bluesky", actor.get("displayName") or actor["handle"],
                                    "", actor["did"], "person", relation, event))

    def collect_list(method, field, limit=100, pages=1, **params):
        for _ in range(pages):
            result = api(method, limit=limit, **params)
            yield from result[field]
            if not result.get("cursor"):
                return
            params["cursor"] = result["cursor"]
        coverage.append(field)

    for actor in collect_list("app.bsky.graph.getFollows", "follows", pages=20, actor=did):
        add(actor, "follow", "")

    def replies(thread):
        stack = list(thread.get("replies", []))
        while stack:
            part = stack.pop()
            if "post" in part:
                yield part["post"]
                stack.extend(part.get("replies", []))

    posts = 0
    for item in collect_list("app.bsky.feed.getAuthorFeed", "feed", limit=50, pages=4,
                             actor=did, filter="posts_no_replies"):
        post = item["post"]
        if post["author"]["did"] != did:
            continue
        created = datetime.fromisoformat(post["record"]["createdAt"].replace("Z", "+00:00"))
        if created < cutoff:
            continue
        if posts == POSTS:
            coverage.append("posts")
            break
        posts += 1
        uri = post["uri"]
        if post.get("likeCount"):
            for like in collect_list("app.bsky.feed.getLikes", "likes", uri=uri):
                add(like["actor"], "reaction", uri)
        if post.get("repostCount"):
            for actor in collect_list("app.bsky.feed.getRepostedBy", "repostedBy", uri=uri):
                add(actor, "repost", uri)
        if post.get("replyCount"):
            thread = api("app.bsky.feed.getPostThread", uri=uri, depth=2, parentHeight=0)["thread"]
            coverage.append("reply depth ≤ 2")
            for reply in replies(thread):
                add(reply["author"], "comment", reply["uri"])
    return did, rows, posts, sorted(set(coverage))


def bluesky(staff, folder):
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW)
    coverage = []
    rows = []
    known = {}
    for person in staff:
        handle = person.get("bluesky")
        if not handle:
            coverage.append(f"{person['name']} · Bluesky: no verified public account.")
            continue
        try:
            did, observations, posts, caps = bsky_rows(person, handle, cutoff)
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
            coverage.append(f"{person['name']} · Bluesky: read failed ({type(exc).__name__}).")
            continue
        known[person["id"]] = did
        rows.extend(asdict(row) for row in observations)
        note = f" Coverage limits: {', '.join(caps)}." if caps else ""
        coverage.append(f"{person['name']} · Bluesky: {posts} public authored posts in the last {WINDOW} days "
                        f"and {sum(row.relation == 'follow' for row in observations)} outgoing follows.{note}")
    put(folder / "observations" / "bluesky.json", {"observations": rows, "coverage": coverage, "staff_accounts": known})
    print(f"Bluesky: {len(known)} accounts read, {len(rows)} observations.")


def bluesky_mutual(folder):
    path = folder / "observations" / "bluesky.json"
    source = json.loads(path.read_text())
    known = source["staff_accounts"]
    result = set()
    coverage = []
    prior_rows = [Observation(**row) for row in source["observations"]]
    for staff_id, did in known.items():
        follows = {row.account for row in prior_rows if row.staff == staff_id and row.relation in ("follow", "mutual_follow")}
        mutual = set()
        params = {"actor": did, "limit": 100}
        try:
            for _ in range(20):
                page = bsky_api("app.bsky.graph.getFollowers", **params)
                mutual.update(follows.intersection(actor["did"] for actor in page["followers"]))
                if not page.get("cursor") or mutual == follows:
                    break
                params["cursor"] = page["cursor"]
            else:
                coverage.append(f"Bluesky follower pages capped for {staff_id}.")
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
            coverage.append(f"Bluesky mutual follow check failed for {staff_id} ({type(exc).__name__}).")
        result.update((staff_id, account) for account in mutual)
    for row in source["observations"]:
        if row["relation"] in ("follow", "mutual_follow"):
            row["relation"] = "mutual_follow" if (row["staff"], row["account"]) in result else "follow"
    source["coverage"] = [line for line in source["coverage"] if not line.startswith("Bluesky mutual:")] + [
        f"Bluesky mutual: verified {len(result)} reciprocal follows across {len(known)} public staff accounts.",
        *(f"Bluesky mutual: {line}" for line in coverage),
    ]
    put(path, source)
    print(f"Bluesky reciprocal follows: {len(result)}.")


def firecrawl(staff, folder, credential):
    if not credential:
        sys.exit("Firecrawl credential unavailable")
    opener = build_opener(NoRedirect)
    coverage = []
    jobs = [(person, network, url) for person in staff
            for network, urls in person.get("public_pages", {}).items() for url in urls]
    blocked_networks = set()
    for person, network, url in jobs:
        if network in blocked_networks:
            coverage.append(f"{person['name']} · {network}: skipped after an HTTP 403 on this network.")
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in HOSTS[network]:
            raise ValueError("Firecrawl requires a public HTTPS URL on the named network")
        request = Request("https://api.firecrawl.dev/v2/scrape",
                          data=json.dumps({"url": url, "formats": ["markdown", "links"],
                                           "proxy": "basic"}).encode(),
                          headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"})
        sleep(3)
        try:
            with opener.open(request, timeout=45) as response:
                result = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            code = getattr(exc, "code", None)
            coverage.append(f"{person['name']} · {network}: Firecrawl request unavailable ({code or type(exc).__name__}).")
            if code == 403:
                blocked_networks.add(network)
            if code in {401, 402, 429}:
                coverage.append(f"Firecrawl stopped after HTTP {code}. Pending pages were skipped.")
                break
            continue
        data = result.get("data", {})
        metadata = data.get("metadata", {})
        final = urlsplit(metadata.get("url") or metadata.get("sourceURL") or url)
        blocked = (not result.get("success") or metadata.get("statusCode", 200) >= 400 or
                   final.netloc not in HOSTS[network] or any(
                       part in final.path.lower() for part in ("/login", "/authwall", "/checkpoint", "/signup")
                   ))
        if blocked or not data.get("markdown"):
            coverage.append(f"{person['name']} · {network}: public page unavailable.")
            continue
        put(folder / "scrapes" / f"{key_for(url)}.json", {
            "staff": person["id"], "network": network, "url": url,
            "markdown": data["markdown"], "links": data.get("links", []),
        })
        coverage.append(f"{person['name']} · {network}: public page fetched. Interaction counts require visible attribution.")
    put(folder / "observations" / "firecrawl-coverage.json", {"observations": [], "coverage": coverage})
    print(f"Firecrawl: {len(coverage)} coverage entries.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("candidates", "bluesky", "bluesky-mutual", "firecrawl"))
    parser.add_argument("data", type=Path)
    options = parser.parse_args()
    staff = json.loads((options.data / "staff.json").read_text())
    if options.mode == "candidates":
        candidate_profiles(staff)
    elif options.mode == "bluesky":
        bluesky(staff, options.data)
    elif options.mode == "bluesky-mutual":
        bluesky_mutual(options.data)
    else:
        firecrawl(staff, options.data, sys.stdin.read().strip())


if __name__ == "__main__":
    main()
