"""Learn how an X account writes replies, so new replies can match the tone.

    python x_style.py fetch --handle someone --max 1000    # scrape replies via Apify
    python x_style.py build --handle someone               # turn them into a style guide

`fetch` runs the Apify actor apidojo/tweet-scraper (Tweet Scraper V2) with the
search `from:<handle> filter:replies`. It prints the estimated cost and asks
before spending; pass --yes to skip the question. Needs APIFY_TOKEN in the
environment or in a .env file next to this script.

`build` writes styles/<handle>/profile.json (numbers) and style.md (a guide for
whoever writes the replies: you or an AI). The guide describes the *style*;
what a reply says must come from the post being answered, and
`x_reply.py lint --style <handle>` rejects drafts that copy a scraped reply.
"""
import argparse
import html
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

ACTOR = "61RPP7dywgiy0JPD0"  # apidojo/tweet-scraper
PRICE_PER_TWEET = 0.0004     # USD, Apify Store price for this actor (Oct 2026)
HERE = Path(__file__).parent
STYLES = HERE / "styles"

STOPWORDS = set("""a an the and or but if so to of in on at for with from by as is are was were be
been it its it's this that these those i you he she we they me my your our their his her them us
do does did not no yes just can will would should could have has had what when where who how why
than then there here about into out up down over more most very also too all any some get got
im i'm dont don't thats that's""".split())


# ---------------------------------------------------------------- Apify

def apify_token():
    if os.environ.get("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APIFY_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    sys.exit("APIFY_TOKEN not found: set it in the environment or in .env (see .env.example)")


def api(method, path, token, body=None):
    req = urllib.request.Request(
        f"https://api.apify.com/v2{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def tweet_date(t):
    return datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y").date()


def run_actor(token, run_input, cap):
    run = api("POST", f"/acts/{ACTOR}/runs?maxTotalChargeUsd={cap}&waitForFinish=60",
              token, run_input)["data"]
    while run["status"] in ("READY", "RUNNING"):
        print(f"  {run['status']}...", flush=True)
        run = api("GET", f"/actor-runs/{run['id']}?waitForFinish=60", token)["data"]
    if run["status"] != "SUCCEEDED":
        sys.exit(f"Apify run {run['id']} ended {run['status']}")
    items = api("GET", f"/datasets/{run['defaultDatasetId']}/items?clean=true&format=json", token)
    return run, items


def fetch(handle, max_items, yes=False):
    """Scrape up to max_items replies, newest first, resuming from replies.jsonl if it exists.

    One X search only reaches back a couple of weeks for busy accounts, so we
    page backwards with `until:<date>` until we have enough or run out.
    """
    handle = handle.lstrip("@")
    out = STYLES / handle
    out.mkdir(parents=True, exist_ok=True)
    path = out / "replies.jsonl"
    replies = ([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
               if path.exists() else [])
    have = {r["id"] for r in replies}
    need = max_items - len(replies)
    if need <= 0:
        print(f"Already have {len(replies)} replies for @{handle}.")
        return replies

    estimate = need * PRICE_PER_TWEET
    cap = round(estimate * 1.25 + 0.05, 2)
    print(f"Scraping {need} more replies by @{handle} (have {len(replies)}): about "
          f"${estimate:.2f}. Spending stops at ${cap:.2f}.")
    if not yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("cancelled")
    token = apify_token()

    # Page backwards: the next query ends on the oldest day we already have.
    until = (min(tweet_date(r) for r in replies) + timedelta(days=1)) if replies else None
    runs, spent = [], 0.0
    while len(replies) < max_items and spent < cap:
        term = f"from:{handle} filter:replies" + (f" until:{until.isoformat()}" if until else "")
        run, items = run_actor(token, {"searchTerms": [term], "sort": "Latest",
                                       "maxItems": max_items - len(replies)},
                               round(cap - spent, 2))
        spent += run.get("usageTotalUsd") or 0
        new = 0
        for t in items:
            author = (t.get("author") or {}).get("userName", "")
            if author.lower() != handle.lower() or not t.get("isReply") or t["id"] in have:
                continue  # the search can return quotes, other people, or overlap
            have.add(t["id"])
            replies.append({k: t.get(k) for k in (
                "id", "url", "text", "createdAt", "lang", "likeCount", "replyCount",
                "retweetCount", "inReplyToId", "inReplyToUsername", "conversationId")})
            new += 1
        runs.append({"run_id": run["id"], "query": term, "returned": len(items), "new": new,
                     "usage_usd": run.get("usageTotalUsd")})
        print(f"  {term!r}: {len(items)} returned, {new} new, total {len(replies)}", flush=True)
        if not items:
            break  # nothing older left
        # Next window ends after the oldest day we have (overlap is de-duplicated),
        # but always earlier than this window so a busy day can't loop forever.
        nxt = min(tweet_date(r) for r in replies) + timedelta(days=1)
        until = nxt if not until or nxt < until else until - timedelta(days=1)

    with path.open("w", encoding="utf-8") as f:
        for r in replies:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    meta = {"handle": handle, "actor": "apidojo/tweet-scraper",
            "fetched": time.strftime("%Y-%m-%d %H:%M"), "replies": len(replies),
            "runs": runs, "usage_usd_this_fetch": round(spent, 3)}
    meta_path = out / "fetch_meta.json"
    history = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["previous"] = history
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k != "previous"}, indent=2))
    return replies


# ---------------------------------------------------------------- style profile

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")


def clean(text):
    """Drop the leading @mentions X adds to replies, links, and extra whitespace."""
    text = html.unescape(text or "").replace("’", "'")
    text = re.sub(r"^(@\w+\s+)+", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def load_replies(handle):
    path = STYLES / handle / "replies.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found: run `python x_style.py fetch --handle {handle}` first")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for r in rows:
        r["clean"] = clean(r["text"])
    # Replies inside their own threads are thread posts, not answers to someone.
    return [r for r in rows if r["clean"] and (r.get("inReplyToUsername") or "").lower() != handle.lower()]


def pct(part, whole):
    return round(100 * part / whole) if whole else 0


def build(handle):
    handle = handle.lstrip("@")
    rows = load_replies(handle)
    texts = [r["clean"] for r in rows]
    n = len(texts)
    words = [len(t.split()) for t in texts]
    q = statistics.quantiles(words, n=4) if n >= 4 else [min(words), statistics.median(words), max(words)]
    letters = [t for t in texts if t[0].isalpha()]
    emojis = Counter(e for t in texts for e in EMOJI_RE.findall(t))
    tokens = [w for t in texts for w in re.findall(r"[a-z][a-z']+", t.lower())]
    bigrams = Counter(zip(tokens, tokens[1:]))
    openers = Counter(t.split()[0].lower().strip(".,!?") for t in texts)

    profile = {
        "handle": handle, "replies": n,
        "words": {"median": statistics.median(words), "p25": q[0], "p75": q[-1]},
        "starts_lowercase_pct": pct(sum(t[0].islower() for t in letters), len(letters)),
        "ends_with_period_pct": pct(sum(t.endswith(".") for t in texts), n),
        "question_pct": pct(sum("?" in t for t in texts), n),
        "exclamation_pct": pct(sum("!" in t for t in texts), n),
        "emoji_pct": pct(sum(bool(EMOJI_RE.search(t)) for t in texts), n),
        "emoji_only_pct": pct(sum(not EMOJI_RE.sub("", t).strip() for t in texts), n),
        "top_emojis": [e for e, _ in emojis.most_common(8)],
        "lowercase_i_pct": pct(sum(bool(re.search(r"(^|\s)i(\s|'|$)", t)) for t in texts),
                               max(1, sum(bool(re.search(r"(^|\s)[iI](\s|'|$)", t)) for t in texts))),
        "all_caps_word_pct": pct(sum(bool(re.search(r"\b[A-Z]{3,}\b", t)) for t in texts), n),
        "dash_pct": pct(sum(bool(re.search(r"[—–]", t)) for t in texts), n),
        "top_openers": [w for w, _ in openers.most_common(12)],
        "top_words": [w for w, _ in Counter(w for w in tokens if w not in STOPWORDS).most_common(40)],
        "top_phrases": [" ".join(b) for b, c in bigrams.most_common(80)
                        if not set(b) <= STOPWORDS and c > 2][:25],
    }

    # Examples for tone: the most-liked replies plus a spread of lengths.
    short = [r for r in rows if len(r["clean"].split()) <= 40]
    by_likes = sorted(short, key=lambda r: r.get("likeCount") or 0, reverse=True)[:12]
    rng = random.Random(7)
    spread = rng.sample(short, min(18, len(short)))
    seen, examples = set(), []
    for r in by_likes + spread:
        if r["clean"] not in seen:
            seen.add(r["clean"])
            examples.append(r)

    out = STYLES / handle
    (out / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "style.md").write_text(style_md(profile, examples), encoding="utf-8")
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    print(f"\nwrote {out / 'profile.json'} and {out / 'style.md'}")


def style_md(p, examples):
    h, w = p["handle"], p["words"]
    lines = [
        f"# How @{h} replies",
        "",
        f"Learned from {p['replies']} of @{h}'s replies on X. Use it for tone and shape only.",
        "",
        "## Rules",
        "",
        "- What the reply says comes from the post you are answering. This guide only sets how it sounds.",
        f"- Never reuse a sentence or distinctive phrase from @{h}'s replies. "
        f"`python x_reply.py lint --style {h}` rejects drafts that do.",
        "- Apply the humanizer skill (github.com/blader/humanizer): no dashes, staged openers, "
        "stock AI words, generic praise, or repeating what the post said.",
        "- Don't claim experiences or facts the reply writer doesn't have.",
        "",
        "## Shape",
        "",
        f"- Length: usually {w['p25']:.0f} to {w['p75']:.0f} words, median {w['median']:.0f}.",
        f"- Starts with a lowercase letter in {p['starts_lowercase_pct']}% of replies; "
        f"writes 'i' in lowercase {p['lowercase_i_pct']}% of the time.",
        f"- Ends with a period in {p['ends_with_period_pct']}%; asks a question in {p['question_pct']}%; "
        f"uses '!' in {p['exclamation_pct']}%.",
        f"- Emoji in {p['emoji_pct']}% of replies ({p['emoji_only_pct']}% are emoji only). "
        f"Most used: {' '.join(p['top_emojis']) or 'none'}.",
        f"- ALL CAPS words in {p['all_caps_word_pct']}%; dashes in {p['dash_pct']}%.",
        "",
        "## Words and openings",
        "",
        f"- Often opens with: {', '.join(p['top_openers'])}",
        f"- Words he leans on: {', '.join(p['top_words'][:25])}",
        f"- Recurring phrases: {', '.join(p['top_phrases']) or 'none stand out'}",
        "",
        "## Examples (for tone only; do not reuse)",
        "",
    ]
    for r in examples:
        to = f" (to @{r['inReplyToUsername']})" if r.get("inReplyToUsername") else ""
        lines.append(f"- {r['clean']}{to}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- copy check

def too_close(draft, handle, threshold=0.75):
    """Return the scraped reply a draft copies (similar overall or sharing 5 words), else None."""
    d = clean(draft).lower()
    d_words = re.findall(r"[a-z0-9']+", d)
    grams = {" ".join(d_words[i:i + 5]) for i in range(len(d_words) - 4)}
    for r in load_replies(handle):
        src = r["clean"].lower()
        if len(d) > 12 and SequenceMatcher(None, d, src).ratio() >= threshold:
            return r["clean"]
        if grams and any(g in " ".join(re.findall(r"[a-z0-9']+", src)) for g in grams):
            return r["clean"]
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="scrape an account's replies with Apify")
    f.add_argument("--handle", required=True)
    f.add_argument("--max", type=int, default=1000, help="how many replies (default 1000)")
    f.add_argument("--yes", action="store_true", help="don't ask before spending")
    f.add_argument("--no-build", action="store_true")
    b = sub.add_parser("build", help="build the style guide from scraped replies")
    b.add_argument("--handle", required=True)
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch(a.handle, a.max, yes=a.yes)
        if not a.no_build:
            build(a.handle)
    else:
        build(a.handle)


if __name__ == "__main__":
    main()
