# Funny posts for X

A separate meme, funny-video and fails queue, adapted from the existing news poster's durable Git history and TweetKit media uploader. It has no news sources, identity discovery, username replacement, news ranking, or automatic reply likes. No cookie, database or posting history was copied from the news repository.

**Current status:** the Telegram sources are `bestmemes`, `memee`, `shitpost`, `MemeWorld`, `funnyanimals` and `thedankestmemes` (the strongest reactions in the 17 October 2026 activity audit). Reddit remains disabled because its audit requests failed. The new X account connection is pending, and no automatic schedule is enabled. Code tests do not establish live X posting or unrestricted Reddit access.

## Choose sources first

Read [the source shortlist and measured activity](docs/SOURCES.md), backed by [the timestamped public-source audit](docs/source-audit.json). The initial five Telegram channels are configured in `sources.json`; review them after the first day of collection. Add or remove bare channel names as needed. Leave `reddit` empty until access and activity are verified. The **Check meme source activity** Actions workflow can repeat the survey from GitHub without account credentials. Errors are reported explicitly. Activity and reactions do not establish that a post is funny or that you have permission to republish it.

## How it works

- One external trigger every **20 minutes**, with no application-level daily posting cap. Each live trigger can post one eligible queued item, even if the previous post was less than 20 minutes ago; the external scheduler controls timing. Unavailable media, an empty queue and uncertain outcomes can prevent a post.
- Collect on every trigger, so the growth in each meme's views and reactions is tracked; then pick at most one meme, fetch its media and publish. Each job exits; nothing sleeps between triggers.
- Telegram photos and downloadable videos work even without a caption. Reddit-hosted images/videos are supported; Reddit DASH audio is merged with the video using FFmpeg when available. External video platforms, albums and protected/unsupported media are skipped.
- **AI judge (only virality matters):** the best-scoring memes are shown to a vision AI (Gemini free tier first, Groq vision as backup). It looks at the image, or a still frame for videos, predicts how viral the post would be (1–10), rejects unfunny, low-quality, NSFW, hateful or promotional memes, and writes a short, punchy caption, or none when the meme works alone. Verdicts are cached; at most 4 new AI calls per run and 250 per day. Without AI keys, the original caption and the numbers-based score are used.
- **Virality score:** views per hour and reactions per view compared with *the same channel's* normal pace, so a standout meme from a small channel beats an average one from a huge channel. Plus freshness (6-hour half-life), a small video bonus, a small penalty for the same channel twice in a row, and the AI's virality rating (8 points per step from 5).
- **Quality bar:** a trigger only posts if the best meme scores at least `MIN_SCORE` (default 70); otherwise it waits for something better. There is no daily cap unless you set `DAILY_LIMIT`.
- **Learning:** about once an hour, live runs read likes, reposts and replies of recent posts and nudge per-channel and video/image weights (bounded 0.7–1.4) toward what the audience engages with.
- Source URLs prevent repeated collection; exact downloaded-media SHA-256 fingerprints stop identical files from being posted twice across sources. Edited, cropped or re-encoded duplicates are not reliably detected.
- Failed media downloads are skipped; memes are never replaced by a text-only post. Promo/casino captions and Reddit NSFW/spoiler posts are filtered. These are metadata filters, not visual moderation.
- Source intake and queued items default to 48 hours, separately configurable. Expired/sent queue media references are removed. Media files are temporary and never committed into history.
- Before public posting, the reservation and media hash are saved to this repository's own `autopost-state` branch. Unknown outcomes pause posting rather than automatically repeating. A stale writer cannot overwrite newer history. Small URL/hash/publication records remain for deduplication.

## Set up the new account

1. Choose sources first.
2. In **this repository**, add the meme account's web-session cookie as the Actions secret `TWEETKIT_COOKIE`. Never paste it in chat or commit it. No cookie was copied or connected automatically. The unofficial TweetKit session method can be rejected or expire.
3. Add the AI keys as Actions secrets in **this repository**: `GEMINI_API_KEY` (free at aistudio.google.com) and optionally `GROQ_API_KEY` (free at console.groq.com). Run the workflow once with **Only test the AI keys/models** checked to confirm they work. Optional variables: `MIN_SCORE` (70), `DAILY_LIMIT` (0 = none), `GEMINI_MODEL` (`gemini-3.5-flash`), `AI_PROVIDER_ORDER` (`gemini,groq`).
4. Open **Actions → Meme scheduler → Run workflow**, choose `main`, check **Create isolated posting history** on the first run only, and leave publishing unchecked. Review the source results and draft. The first initialization writes only this repository's isolated history branch.
5. Run again with publishing checked to test one media post. Keep initialization unchecked afterward. Check the actual X profile before scheduling.
6. Create a fine-grained GitHub token for this repository with **Actions: read and write** and store it only in cron-job.org's request header.
7. Configure one cron-job.org job, initially disabled:

   - Schedule: `*/20 * * * *`
   - Method: `POST`
   - URL: `https://api.github.com/repos/justsomeoneretarded/fnuny-posts/actions/workflows/memes.yml/dispatches`
   - Headers: `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `Content-Type: application/json`, `X-GitHub-Api-Version: 2022-11-28`
   - Body: `{"ref":"main","inputs":{"live":true,"initialize":false}}`

8. Test once. HTTP 204 only confirms dispatch acceptance; inspect Actions for the result. Enable the external job after a successful test. No computer needs to remain on. There is no second built-in cron schedule to disable.

Reddit public JSON can block cloud/datacenter requests and may require approved Reddit API access; the collector reports the failure. It does not bypass restrictions. Telegram-only operation remains possible. Account cookie expiry, token expiry, source outages, X restrictions and media limits can interrupt automation. GitHub runner minutes still apply: about 2,160 runs per 30-day month, with each job subject to GitHub billing rules.

## Development

Python 3.12 and FFmpeg/FFprobe are required for full video support. In this checkout:

```sh
python -m venv .local/runtime
.local/runtime/bin/python -m pip install -r requirements.txt
.local/runtime/bin/python -m unittest discover -s tests -v
```

The pinned TweetKit commit is retained from the working media uploader. The new worker and collectors have their own tests; durable-history tests exercise actual temporary Git repositories, and the video test merges a generated audio/video clip using FFmpeg. Full live publishing remains a separate account-connected test.

**Uncertain post:** disable the external cron job, inspect X, and reconcile the saved reservation before re-enabling. Do not reset posting history or repeatedly rerun an uncertain send.
