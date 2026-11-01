"""Reply to a post in the X Android app over adb, with per-step timing.

    python x_reply.py scan                                  # list posts on screen
    python x_reply.py reply --handle someone --text "how long did that take you?"
    python x_reply.py reply --composer-open --handle someone --text "..."
    add --dry-run to type the reply without sending it

Works on the post that is on screen now (nudging it into reach if needed), so
read -> decide -> reply happens in one pass instead of re-searching a feed that
keeps refreshing. Emojis are tapped in the keyboard's emoji panel by their
accessibility label, because `adb shell input text` can only send ASCII.
Tested on a Galaxy S22 (1080x2340) with Microsoft SwiftKey.
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

X_PKG = "com.twitter.android"
SAFE_TOP, SAFE_BOTTOM = 420, 1990   # taps outside this hit X's tab bar or Android's nav bar
EMOJI_KEYWORDS = {"😭": "crying", "💀": "skull", "🎉": "party", "👀": "eyes",
                  "😂": "joy", "🔥": "fire", "🙏": "pray", "😅": "sweat"}
LOG = Path(__file__).with_name("reply_runs.jsonl")

# Pacing presets: gap = seconds from one reply's start to the next (random in range),
# a break of break_min minutes after every break_every replies, and a daily cap.
PRESETS = {
    "steady": {"gap": (90, 150), "break_every": 15, "break_min": 10, "daily_cap": 120},
    "active": {"gap": (60, 100), "break_every": 20, "break_min": 10, "daily_cap": 200},
    "fast":   {"gap": (50, 80),  "break_every": 25, "break_min": 10, "daily_cap": 250},
    "turbo":  {"gap": (40, 70),  "break_every": 30, "break_min": 15, "daily_cap": 300},
}
EMOJI_EVERY = 7  # at most one reply in seven carries an emoji

# Text X shows when it is throttling or flagging the account: stop on sight.
WARNING_SIGNS = ("automated", "unusual activity", "temporarily limited", "rate limit",
                 "over the daily limit", "try again later", "something went wrong",
                 "reply was hidden", "locked")
# Whole words only, so a post saying "blocked" doesn't read as "locked".
WARNING_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, WARNING_SIGNS)) + r")\b")


# ---------------------------------------------------------------- humanizer lint
# Mechanical checks from the humanizer skill (github.com/blader/humanizer), plus
# the reply-specific rule from its §26: don't repeat what the post already said.

AI_WORDS = ("additionally", "align with", "bolstered", "crucial", "deep dive", "delve",
            "enduring", "enhance", "garner", "interplay", "intricate", "landscape",
            "meticulous", "pivotal", "robust", "showcase", "tapestry", "testament",
            "underscore", "valuable", "vibrant", "game changer", "game-changer")
STAGED_OPENERS = ("honestly?", "look,", "here's the thing", "the thing is", "let's be honest",
                  "real talk", "let's dive", "let's break", "quick note", "heads up", "actually,")
CHATBOT_RESIDUE = ("great question", "i hope this helps", "certainly", "of course!",
                   "you're absolutely right", "let me know", "great post", "great thread",
                   "love this", "this is gold", "well said", "so true", "thanks for sharing",
                   "couldn't agree more", "this is huge", "absolutely")


APOSTROPHES = re.compile(r"['’‘\"“”]")
# Contractions typed without the apostrophe: the keyboard's autocorrect puts it back.
BARE_CONTRACTIONS = ("whats", "lets", "dont", "cant", "wont", "im", "youre", "thats", "didnt",
                     "isnt", "doesnt", "youd", "theyre", "ive", "youll", "theyll", "shouldnt",
                     "wouldnt", "couldnt", "aint", "hes", "shes", "wasnt", "arent", "havent")


def _numbers(s):
    out = set()
    for num, k in re.findall(r"(\d[\d,.]*)\s*([kKmM]?)\b", s):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        v *= {"k": 1e3, "m": 1e6}.get(k.lower(), 1)
        if v >= 10:  # "year 1" vs "$1 online" isn't repetition
            out.add(v)
    return out


def humanizer_lint(text, post_text=""):
    """Return (blocking problems, soft warnings) for a reply."""
    t = text.lower()
    hard, soft = [], []
    if re.search(r"[—–]| -- ", text):
        hard.append("dash (§8)")
    if APOSTROPHES.search(text):
        hard.append("apostrophe or quote mark: rephrase without contractions")
    hard += [f"'{w}' gets autocorrected to a contraction: rephrase" for w in BARE_CONTRACTIONS
             if re.search(rf"\b{w}\b", t)]
    hard += [f"AI word '{w}' (§12)" for w in AI_WORDS if re.search(rf"\b{re.escape(w)}\b", t)]
    hard += [f"staged opener '{o}' (§4)" for o in STAGED_OPENERS if t.startswith(o)]
    hard += [f"chatbot / reply-guy residue '{r}' (§22)" for r in CHATBOT_RESIDUE if r in t]
    if re.search(r"\bnot (just|only|merely)\b.*\bbut\b|\bit'?s not\b.+\bit'?s\b", t):
        hard.append("not X but Y (§1)")
    if re.search(r"\b[A-Z]{5,}\b", text):
        hard.append("ALL CAPS emphasis (§2)")
    if post_text:
        if _numbers(text) & _numbers(post_text):
            hard.append("repeats a number from the post (§26)")
        words = re.findall(r"[a-z0-9']+", t)
        post = " ".join(re.findall(r"[a-z0-9']+", post_text.lower()))
        if any(" ".join(words[i:i + 4]) in post for i in range(len(words) - 3)):
            hard.append("repeats 4+ words from the post (§26)")
    if re.search(r"\w+, \w+,? and \w+", t):
        soft.append("possible forced triad (§6)")
    if re.search(r"[“”‘’]", text):
        soft.append("curly quotes (§21)")
    return hard, soft


def has_emoji(s):
    return any(ord(ch) > 0x2000 and ch not in "’‘“”" for ch in s)


def adb(*args):
    return subprocess.run(["adb", *args], capture_output=True, text=True,
                          encoding="utf-8").stdout


def shell(cmd):
    return adb("shell", cmd)


def tap(node_or_xy):
    x, y = node_or_xy["center"] if isinstance(node_or_xy, dict) else node_or_xy
    shell(f"input tap {x} {y}")


def dump():
    """All windows (app + keyboard) as dicts: text, desc, cls, pkg, rid, bounds, center."""
    shell("uiautomator dump --windows /sdcard/ui.xml")
    raw = adb("exec-out", "cat /sdcard/ui.xml")
    nodes = []
    for n in ET.fromstring(raw[raw.index("<"):]).iter("node"):
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
        nodes.append({"text": n.get("text", ""), "desc": n.get("content-desc", ""),
                      "cls": n.get("class", ""), "pkg": n.get("package", ""),
                      "rid": n.get("resource-id", "").split("/")[-1],
                      "bounds": (x1, y1, x2, y2),
                      "center": ((x1 + x2) // 2, (y1 + y2) // 2)})
    return nodes


def first(nodes, pred):
    return next((n for n in nodes if pred(n)), None)


def poll(pred, timeout=5.0, every=0.25):
    end = time.time() + timeout
    while True:
        nodes = dump()
        hit = first(nodes, pred)
        if hit or time.time() > end:
            return hit, nodes
        time.sleep(every)


def is_composer(n):
    return n["pkg"] == X_PKG and n["cls"] == "android.widget.EditText"


_ime_pkg = None


def is_keyboard(n):
    global _ime_pkg
    if _ime_pkg is None:  # e.g. com.touchtype.swiftkey/com.touchtype.KeyboardService
        _ime_pkg = shell("settings get secure default_input_method").strip().split("/")[0]
    return n["pkg"] == _ime_pkg and n["cls"] != "android.widget.EditText"


def composer_text(nodes):
    box = first(nodes, is_composer)
    return box["text"] if box else None


# ---------------------------------------------------------------- finding posts

def indented(head, app):
    """Quoted posts sit in an inset card: their author row starts well right of the name column."""
    row = [n for n in app if n["text"] and abs(n["bounds"][1] - head["bounds"][1]) < 12]
    return min(n["bounds"][0] for n in row) >= 230


def visible_posts(nodes):
    """Posts on screen: handle, text, and the Reply button that belongs to them."""
    app = [n for n in nodes if n["pkg"] == X_PKG]
    heads = [n for n in app if re.fullmatch(r"@\w+", n["text"])]
    replies = [n for n in app if n["desc"] == "Reply"]
    posts = []
    for i, h in enumerate(heads):
        next_top = heads[i + 1]["bounds"][1] if i + 1 < len(heads) else 10**9
        body = [n["text"] for n in app if n["text"] and len(n["text"]) > 25
                and h["bounds"][1] < n["bounds"][1] < next_top]
        btn = first(sorted(replies, key=lambda n: n["bounds"][1]),
                    lambda n: n["bounds"][1] > h["bounds"][1])
        posts.append({"handle": h["text"][1:], "text": body[0] if body else "",
                      "is_ad": any(n["text"] == "Ad" and h["bounds"][1] - 10 <= n["bounds"][1] < next_top
                                   for n in app),
                      "quoted": (bool(posts) and btn is not None and posts[-1]["reply_btn"] is btn)
                      or indented(h, app),
                      "reply_btn": btn})
    # A quoted post shares its Reply row with the post quoting it: that row
    # replies to the outer post, so the quoted one gets no button of its own.
    for p in posts:
        if p["quoted"]:
            p["reply_btn"] = None
    return posts


def reachable(btn):
    return btn and SAFE_TOP < btn["center"][1] < SAFE_BOTTOM


def reply_button_on_screen(handle):
    """Reply button for @handle's post on the current screen, nudged into reach.

    A tall post can't show its @handle and its Reply row at once, so after the
    nudge we follow the button by position rather than by handle.
    """
    post = first(visible_posts(dump()), lambda p: p["handle"].lower() == handle.lower())
    if not post:
        return None
    btn = post["reply_btn"]
    if reachable(btn):
        return btn
    if not btn:  # Reply row not laid out yet: bring more of the post up
        shell("input swipe 540 1700 540 1100 900")
        time.sleep(0.8)
        return reply_button_on_screen(handle)
    target = 1700
    shift = btn["center"][1] - target
    shell(f"input swipe 540 1900 540 {1900 - shift} 1200")
    for _ in range(4):  # the list keeps gliding for a moment after the swipe
        time.sleep(0.5)
        near = [n for n in dump() if n["pkg"] == X_PKG and n["desc"] == "Reply"
                and abs(n["center"][1] - target) < 400 and reachable(n)]
        if near:
            return min(near, key=lambda n: abs(n["center"][1] - target))
    return None


# ---------------------------------------------------------------- typing

def split_segments(text):
    """'lets go 🎉 ok' -> [('ascii','lets go '), ('emoji','🎉'), ('ascii',' ok')]"""
    segs = []
    for ch in text:
        if ord(ch) < 128:
            if segs and segs[-1][0] == "ascii":
                segs[-1] = ("ascii", segs[-1][1] + ch)
            else:
                segs.append(("ascii", ch))
        elif ord(ch) in (0xFE0F, 0x200D) and segs:  # variation selector / joiner
            segs[-1] = ("emoji", segs[-1][1] + ch)
        else:
            segs.append(("emoji", ch))
    return segs


def type_ascii(s):
    for i, line in enumerate(s.split("\n")):
        if i:
            shell("input keyevent 66")  # Enter: `input text` can't send newlines
        if line:
            quoted = line.replace(" ", "%s").replace("'", "'\\''")  # %s = space for `input text`
            shell(f"input text '{quoted}'")


def keyboard_shown():
    """~0.2 s check (vs ~3 s for a UI dump) that a text field has the keyboard up."""
    return "mInputShown=true" in shell("dumpsys input_method | grep mInputShown")


def x_in_front():
    """True if X is the focused app; stops us typing into whatever else the user opened."""
    return X_PKG in shell("dumpsys window | grep mCurrentFocus")


def wait_keyboard(shown=True, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if keyboard_shown() == shown:
            return True
        time.sleep(0.2)
    return False


def clear_composer():
    shell("input keycombination 113 29")  # Ctrl+A
    shell("input keyevent 67")             # Delete


def insert_emoji(emoji):
    """Open the emoji panel, tap the emoji (searching for it if needed), verify."""
    key, _ = poll(lambda n: is_keyboard(n) and n["desc"] == "Emoji"
                  and n["bounds"][1] > 1900, timeout=3)
    if not key:
        raise RuntimeError("keyboard emoji key not found")
    tap(key)
    btn, nodes = poll(lambda n: is_keyboard(n) and n["desc"] == emoji, timeout=1.5)
    if not btn:
        search = first(nodes, lambda n: n["pkg"] == _ime_pkg and n["cls"] == "android.widget.EditText")
        if not search:
            raise RuntimeError("emoji search box not found")
        tap(search)
        time.sleep(0.4)
        type_ascii(EMOJI_KEYWORDS.get(emoji, ""))
        btn, nodes = poll(lambda n: is_keyboard(n) and n["desc"] == emoji, timeout=3)
        if not btn:
            raise RuntimeError(f"{emoji} not offered by the emoji search")
    tap(btn)
    ok, nodes = poll(lambda n: is_composer(n) and n["text"].rstrip().endswith(emoji), timeout=2)
    if not ok:
        raise RuntimeError(f"{emoji} did not land in the reply box")
    back_to_letters(nodes)


def back_to_letters(nodes):
    """Close the emoji panel / search so the next ASCII goes into the reply box."""
    for _ in range(3):
        back = first(nodes, lambda n: is_keyboard(n) and n["desc"] == "Back")
        if not back:
            return
        tap(back)
        time.sleep(0.4)
        nodes = dump()


def fill_composer(text):
    clear_composer()
    after_emoji = False
    for kind, s in split_segments(text):
        if kind == "ascii":
            if after_emoji:
                s = s.lstrip(" ")  # the keyboard already added a space after the emoji
            type_ascii(s)
        else:
            insert_emoji(s)
        after_emoji = kind == "emoji"
    time.sleep(0.3)


def discard_composer():
    """Empty and close an open reply/post box, answering X's save-draft prompt if it shows."""
    if not keyboard_shown():
        return
    clear_composer()
    time.sleep(0.3)
    close = first(dump(), lambda n: n["pkg"] == X_PKG and n["desc"] == "Close")
    if close:
        tap(close)
        time.sleep(1.0)
        nodes = dump()
        discard = first(nodes, lambda n: n["text"].lower() in ("discard", "delete"))
        if discard:
            tap(discard)


def normalize(s):
    """Compare loosely: autocorrect may add apostrophes or capitalise the first letter."""
    return re.sub(r"\s+", " ", (s or "").replace("'", "").replace("’", "")).strip().lower()


# ---------------------------------------------------------------- main flow

def reply(handle, text, post=True, composer_open=False, btn=None):
    steps, last = {}, [time.perf_counter()]

    def mark(name):
        now = time.perf_counter()
        steps[name] = round(now - last[0], 2)
        last[0] = now

    result = {"handle": handle, "text": text, "posted": False,
              "started": datetime.now().isoformat(timespec="seconds")}
    try:
        if not x_in_front():
            raise RuntimeError("X is not the app in front; stopping so nothing is typed elsewhere")
        if not composer_open:
            btn = btn or reply_button_on_screen(handle)
            mark("find_post")
            if not btn:
                raise RuntimeError(f"@{handle} is not on screen")
            tap(btn)
        # Never type unless a text field has focus: on the feed, letters are shortcuts.
        if not wait_keyboard(True) or not x_in_front():
            raise RuntimeError("reply box did not open")
        mark("open_composer")

        fill_composer(text)
        mark("type_text")

        nodes = dump()  # one read checks both who we reply to and what we typed
        hdr = first(nodes, lambda n: n["text"].startswith("Replying to"))
        typed = composer_text(nodes)
        result["typed"] = typed
        mark("verify")
        if not hdr or f"@{handle.lower()}" not in hdr["text"].lower():
            raise RuntimeError(f"composer is not replying to @{handle}: {hdr and hdr['text']!r}")
        # "Replying to @a and @b" means the target is itself someone's reply: skip those.
        if len(set(re.findall(r"@\w+", hdr["text"].lower()))) > 1:
            raise RuntimeError(f"@{handle}'s post is a reply in a conversation: {hdr['text']!r}")
        if normalize(typed) != normalize(text):
            raise RuntimeError(f"text mismatch: typed {typed!r}, wanted {text!r}")
        if APOSTROPHES.search(typed or "") and not APOSTROPHES.search(text):
            raise RuntimeError(f"autocorrect added an apostrophe or quote: {typed!r}")

        if post:
            send = first(nodes, lambda n: n["pkg"] == X_PKG and n["desc"] == "Reply"
                         and n["bounds"][1] < 300)
            tap(send)
            result["posted"] = wait_keyboard(False, timeout=6)  # composer closes on send
            mark("send")
    except Exception as e:
        result["error"] = str(e)
        if x_in_front():
            discard_composer()
    result["steps"] = steps
    result["total_s"] = round(sum(steps.values()), 2)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def phone_locked():
    return "isKeyguardShowing=true" in shell("dumpsys window | grep isKeyguardShowing")


def open_new_post():
    """Bring X to the front and open the new-post composer from the Home tab."""
    if not x_in_front():
        shell(f"monkey -p {X_PKG} -c android.intent.category.LAUNCHER 1")
        time.sleep(2.5)
    for _ in range(3):
        nodes = dump()
        btn = first(nodes, lambda n: n["pkg"] == X_PKG and n["desc"] == "Post"
                    and n["bounds"][1] > 1500)  # the floating button, not the composer's
        if btn:
            tap(btn)
            return wait_keyboard(True) and x_in_front()
        home = first(nodes, lambda n: n["pkg"] == X_PKG and n["desc"] == "Home")
        if home:
            tap(home)
        else:
            shell("input keyevent 4")
        time.sleep(1.2)
    return False


def new_post(text, post=True):
    steps, last = {}, [time.perf_counter()]

    def mark(name):
        now = time.perf_counter()
        steps[name] = round(now - last[0], 2)
        last[0] = now

    result = {"kind": "post", "text": text, "posted": False,
              "started": datetime.now().isoformat(timespec="seconds")}
    try:
        if phone_locked():
            raise RuntimeError("phone is locked")
        if not open_new_post():
            raise RuntimeError("could not open the post composer")
        mark("open_composer")
        fill_composer(text)
        mark("type_text")
        nodes = dump()
        typed = composer_text(nodes)
        result["typed"] = typed
        mark("verify")
        if first(nodes, lambda n: n["text"].startswith("Replying to")):
            raise RuntimeError("composer is a reply, not a new post")
        if normalize(typed) != normalize(text):
            raise RuntimeError(f"text mismatch: typed {typed!r}, wanted {text!r}")
        if post:
            send = first(nodes, lambda n: n["pkg"] == X_PKG and n["desc"] == "Post"
                         and n["bounds"][1] < 300)
            tap(send)
            result["posted"] = wait_keyboard(False, timeout=6)
            mark("send")
        else:  # dry run: leave nothing behind (an empty composer closes without a prompt)
            clear_composer()
            close = first(dump(), lambda n: n["pkg"] == X_PKG and n["desc"] == "Close")
            tap(close)
    except Exception as e:
        result["error"] = str(e)
    result["steps"] = steps
    result["total_s"] = round(sum(steps.values()), 2)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def run_schedule(path, grace_min=15, poll_s=20):
    """Post each {"at": "YYYY-MM-DD HH:MM", "text": ...} when due; status is written back to the file.

    A failed attempt (phone locked, user typing in another app...) is retried until
    the post is `grace_min` minutes late, then marked missed.
    """
    path = Path(path)
    while True:
        items = json.loads(path.read_text(encoding="utf-8"))
        open_items = [it for it in items if not it.get("status")]
        if not open_items:
            log({"schedule": "nothing left to post"})
            return
        now = datetime.now()
        for it in open_items:
            at = datetime.fromisoformat(it["at"])
            if at > now:
                continue
            if (now - at).total_seconds() / 60 > grace_min:
                it["status"] = "missed"
            elif keyboard_shown() and not x_in_front():
                it["last_error"] = "user is typing in another app"
            else:
                hard, _ = humanizer_lint(it["text"])
                if hard and not it.get("force"):
                    it["status"] = "blocked: " + "; ".join(hard)
                else:
                    r = new_post(it["text"])
                    if r["posted"]:
                        it["status"], it["posted_at"] = "posted", r["started"]
                    else:
                        it["last_error"] = r.get("error", "not confirmed")
            log({"at": it["at"], "status": it.get("status", "retrying"),
                 "error": it.get("last_error")})
            path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        nxt = min((datetime.fromisoformat(it["at"]) for it in items if not it.get("status")),
                  default=now)
        time.sleep(max(1, min(poll_s, (nxt - datetime.now()).total_seconds())))


def swipe_page(down=True):
    shell("input swipe 540 1800 540 700 600" if down else "input swipe 540 700 540 1800 600")
    time.sleep(0.8)


def scan_feed(screens=1, rewind=True):
    """Collect posts from the next `screens` screens, then scroll back above them."""
    seen, out = set(), []
    for i in range(screens):
        for p in visible_posts(dump()):
            key = (p["handle"], p["text"][:40])
            if key not in seen and not p["quoted"]:
                seen.add(key)
                out.append(p)
        if i < screens - 1:
            swipe_page()
    if rewind:
        # Exactly as many pages as we scrolled: one more would pull-to-refresh at the
        # top of the feed and replace the posts we just read.
        for _ in range(screens - 1):
            swipe_page(down=False)
    return out


def posted_today():
    if not LOG.exists():
        return 0
    today = date.today().isoformat()
    return sum(1 for line in LOG.read_text(encoding="utf-8").splitlines()
               if '"posted": true' in line and f'"started": "{today}' in line)


def last_posted_time():
    """Epoch seconds of the most recent posted reply or post in the log, or None."""
    if not LOG.exists():
        return None
    times = [json.loads(line).get("started") for line in LOG.read_text(encoding="utf-8").splitlines()
             if '"posted": true' in line]
    times = [t for t in times if t]
    return datetime.fromisoformat(max(times)).timestamp() if times else None


def in_active_hours(hours):
    if not hours:
        return True
    start, end = hours.split("-")
    return start <= datetime.now().strftime("%H:%M") < end


def emoji_budget_problems(items):
    """At most one emoji in any EMOJI_EVERY consecutive replies, counting the posting log."""
    history = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("posted"):
                history.append(entry["text"])
    window = [has_emoji(t) for t in history[-(EMOJI_EVERY - 1):]]
    problems = []
    for it in items:
        if has_emoji(it["text"]) and any(window):
            problems.append(f"@{it['handle']}: emoji within {EMOJI_EVERY} replies of the last one")
        window = (window + [has_emoji(it["text"])])[-(EMOJI_EVERY - 1):]
    return problems


def log(entry):
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def batch(items, post=True, preset=None, hours=None, max_screens=14, force=False, style=None):
    """Reply to each {handle, text} in feed order, walking down the feed once."""
    pace = PRESETS.get(preset)
    problems = emoji_budget_problems(items)
    if problems and not force:
        log({"error": "emoji budget", "details": problems})
        return []
    pending, results, screens, done = list(items), [], 0, 0
    next_start = time.time()
    if pace:  # keep the gap across runs too: time it from the last reply in the log
        last = last_posted_time()
        if last:
            next_start = max(next_start, last + random.uniform(*pace["gap"]))
    while pending and screens <= max_screens:
        if not x_in_front():
            log({"error": "X left the foreground; batch stopped"})
            break
        if keyboard_shown():  # a box is still open; swiping now would type into it
            log({"error": "a text box is open; batch stopped"})
            break
        if pace and posted_today() >= pace["daily_cap"]:
            log({"stopped": f"daily cap {pace['daily_cap']} reached"})
            break
        if not in_active_hours(hours):
            log({"stopped": f"outside active hours {hours}"})
            break
        nodes = dump()
        warning = first(nodes, lambda n: WARNING_RE.search(n["text"].lower())
                        and len(n["text"]) < 200)
        if warning:
            log({"stopped": "X shows a warning", "text": warning["text"]})
            break
        acted = False
        for p in visible_posts(nodes):
            item = first(pending, lambda it: it["handle"].lower() == p["handle"].lower()
                         and it.get("match", "").lower() in p["text"].lower())
            if not item or p["quoted"]:
                continue
            pending.remove(item)
            acted = True
            hard, soft = humanizer_lint(item["text"], p["text"])
            if style:
                from x_style import too_close
                copied = too_close(item["text"], style)
                if copied:
                    hard.append(f"too close to @{style}'s reply: {copied[:80]!r}")
            if hard and not force:
                log({"handle": item["handle"], "skipped": "humanizer", "problems": hard})
                break
            if pace:
                time.sleep(max(0, next_start - time.time()))
                next_start = time.time() + random.uniform(*pace["gap"])
            btn = p["reply_btn"] if reachable(p["reply_btn"]) else None
            r = reply(item["handle"], item["text"], post=post, btn=btn)
            if soft:
                r["lint_warnings"] = soft
            log(r)
            results.append(r)
            done += r.get("posted", False)
            if pace and r.get("posted") and done % pace["break_every"] == 0 and pending:
                log({"break_minutes": pace["break_min"]})
                time.sleep(pace["break_min"] * 60)
                next_start = time.time()
            break  # the screen may have moved: read it again
        if not acted:
            swipe_page()
            screens += 1
    for it in pending:
        log({"handle": it["handle"], "error": "not found on the feed"})
    return results


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list the posts on the next N screens")
    s.add_argument("--screens", type=int, default=1)
    s.add_argument("--no-rewind", action="store_true")
    b = sub.add_parser("batch", help="reply to a JSON list of {handle, text} in feed order")
    b.add_argument("file")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--preset", choices=sorted(PRESETS), help="pacing; omit for no pacing")
    b.add_argument("--hours", help="active hours, e.g. 09:00-21:00")
    b.add_argument("--force", action="store_true", help="post even if the humanizer lint objects")
    b.add_argument("--style", help="handle whose style guide was used; blocks copied replies")
    po = sub.add_parser("post", help="publish a new post now")
    po.add_argument("--text", required=True, help="use \\n for line breaks")
    po.add_argument("--dry-run", action="store_true", help="type it, then clear and close")
    po.add_argument("--force", action="store_true", help="post even if the humanizer lint objects")
    sc = sub.add_parser("schedule", help="run a posting queue: JSON list of {at, text}")
    sc.add_argument("file")
    sc.add_argument("--grace", type=int, default=15, help="minutes late before a post is missed")
    lt = sub.add_parser("lint", help="check reply text against the humanizer rules")
    lt.add_argument("text")
    lt.add_argument("--post", default="", help="the post being replied to")
    lt.add_argument("--style", help="also check the draft doesn't copy this handle's replies")
    r = sub.add_parser("reply")
    r.add_argument("--handle", required=True)
    r.add_argument("--text", required=True)
    r.add_argument("--dry-run", action="store_true", help="type it but don't send")
    r.add_argument("--composer-open", action="store_true",
                   help="the reply box for --handle is already open")
    a = ap.parse_args()
    if a.cmd == "scan":
        for p in scan_feed(a.screens, rewind=a.screens > 1 and not a.no_rewind):
            print(json.dumps({"handle": p["handle"], "ad": p["is_ad"], "text": p["text"][:280]},
                             ensure_ascii=False))
    elif a.cmd == "batch":
        items = json.loads(Path(a.file).read_text(encoding="utf-8"))
        batch(items, post=not a.dry_run, preset=a.preset, hours=a.hours, force=a.force,
              style=a.style)
    elif a.cmd == "post":
        text = a.text.replace("\\n", "\n")
        hard, _ = humanizer_lint(text)
        if hard and not a.force:
            log({"skipped": "humanizer", "problems": hard})
        else:
            log(new_post(text, post=not a.dry_run))
    elif a.cmd == "schedule":
        run_schedule(a.file, grace_min=a.grace)
    elif a.cmd == "lint":
        hard, soft = humanizer_lint(a.text, a.post)
        if a.style:
            from x_style import too_close
            copied = too_close(a.text, a.style)
            if copied:
                hard.append(f"too close to @{a.style}'s reply: {copied[:80]!r}")
        print(json.dumps({"ok": not hard, "problems": hard, "warnings": soft}, ensure_ascii=False))
    else:
        out = reply(a.handle, a.text, post=not a.dry_run, composer_open=a.composer_open)
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
