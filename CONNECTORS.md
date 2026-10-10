# Connectors and external services

| Service | Used for | Setting | Required | How to get it |
|---|---|---|---|---|
| [fal.ai](https://fal.ai) | Images (Seedream 4.5, Nano Banana Pro), video (Seedance 2.0, Kling 3.0; Veo 3.1 Fast until fal retires it on 2026-10-22), music | `fal_key` | Yes | Dashboard → Keys. Turn on **Auto top-up**: a request that finishes while the account is locked for low balance cannot be downloaded later. |
| [ElevenLabs](https://elevenlabs.io) | Japanese narration (`eleven_v3`, timestamps), music | `elevenlabs_api_key`, `narrator_voice_id` | Yes | Settings → API keys. Add a native Japanese voice from the Voice Library and copy its id. |
| [Threads API](https://developers.facebook.com/docs/threads) | Scheduled posting, insights | `threads_access_token`, `threads_user_id` (optional) | For posting | Create a Meta app with the Threads use case, generate a long-lived token. `threads_post.py refresh-token` extends it. |
| AWS S3 | Handing a local video to the Threads API through a 1-hour signed URL | `media_bucket`, `aws_profile`, `aws_region` | For auto-posting video | `python3 scripts/media_host.py --create-bucket` creates a private, encrypted bucket whose objects expire after 1 day. |
| HyperFrames (npm) | Rendering captions, CTA and audio into the final MP4 | — | Yes | Runs through `npx`; needs Node.js 18+. |

## Bundled connector (MCP)

The plugin ships a small local MCP server, `studio`, with three tools:

- `studio_status` — which settings are set (masked) and which tools are installed
- `studio_save_settings` — copies the values from the plugin configuration screen into the local config file the scripts read
- `studio_paths` — plugin, scripts, config and workspace paths

It runs on your computer, so in Claude Cowork it is available only when the session runs locally. In that case use `setup/setup.html` and put `tvs-config.json` in the connected folder instead.

## Cost guide (October 2026, check the provider pages before large runs)

- Video: Seedance 2.0 720p about $0.30/s (billed with audio even when muted), Kling 3.0 about $0.11/s without audio. Exact rates per model live in `scripts/gen_video.py` and are printed by `--dry-run`.
- Images: Seedream 4.5 about $0.04 per image.
- A 15-second story (3–5 cuts) typically costs $5–10 including one round of fixes.
