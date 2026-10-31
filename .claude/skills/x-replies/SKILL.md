---
name: x-replies
description: Write and post X (Twitter) replies through the user's Android phone with x_reply.py. Use when the user asks to reply to posts on their feed, reply in someone's style, post, or schedule posts.
---

# Replying on X through the phone

The phone is connected over USB with debugging on, and the X app is open on the Home feed.

## Steps

1. Read the feed: `python x_reply.py scan --screens 4`. It scrolls down, lists the posts, and scrolls back up.
   Skip ads (`"ad": true`) and engagement bait ("reply GUIDE and I'll DM you"). Posts ending in "Show more"
   are cut off: open the post and read all of it before writing a reply, because the end often changes the meaning.
2. If the user picked a style, read `styles/<handle>/style.md` first. Match its length, casing, punctuation and
   emoji habits. Never reuse its example sentences.
3. Write one reply per post, following the humanizer skill (github.com/blader/humanizer):
   - say something new: a question, a reaction or a specific thought. Never restate the post or its numbers.
   - no dashes, no staged openers ("Honestly?", "Here's the thing"), no stock AI words, and no generic praise ("great post").
   - don't claim experiences or facts the user doesn't have.
   - at most one reply in seven gets an emoji. The batch command enforces this using the posting log.
   - no apostrophes or quote marks, and no contractions typed without them ("whats", "dont"):
     the keyboard autocorrects those back. Rephrase instead ("what would you cut first").
   - sound like a person scrolling, not an interviewer: mostly quick takes, jokes, mild disagreement
     or a specific detail from the post. At most one reply in three is a question, and never the
     "what's the first X you'd Y?" kind.
   - reply only to original posts. Skip anything that is itself a reply, and never reply twice in one
     conversation. The script refuses when the reply box says "Replying to @a and @b".
4. Save the replies to `batches/<date>_<n>.json` as `[{"handle": "...", "match": "a few words of the post", "text": "..."}]`,
   in feed order.
5. Show the user the replies, then run
   `python x_reply.py batch batches/<file>.json --preset steady [--style <handle>]`.
   The batch skips any reply that fails the humanizer lint or copies the style source, and it stops on any X warning.
6. Report what was posted, what was skipped and why, and the timings from `reply_runs.jsonl`.

## Don't

- Don't type when X isn't in front or the reply box isn't open. The script checks this; keep it that way.
- Don't raise the pace above the preset the user chose, and don't use `--force` unless the user asks for it.
