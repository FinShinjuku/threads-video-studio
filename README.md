# threads-video-studio

A Claude plugin (Claude Code and Claude Cowork) for making short vertical AI videos for Threads —
anime/illustration style, Japanese narration, captions, BGM and a call-to-action — and for scheduling,
auto-posting and reviewing them.

Threads 向けの縦型 AI 動画（アニメ調・日本語ナレ・字幕・BGM・誘導ボタン）を作り、予約投稿して振り返るための Claude プラグインです。

## What it does

```
plan → storyboard → [approve] → narration → character sheet & start frames
     → draft cuts → frame-level QA → final cuts → captions, BGM, CTA → [approve]
     → [approve post] → scheduled auto-post → 48h / 7d review → next plan
```

- **Three human gates.** Nothing that costs money is generated before the storyboard is approved,
  and nothing is posted before the post itself is approved.
- **Short by design.** Story videos 10–15 s (about 3 cuts), introductions 20–30 s. One action and one
  camera move per cut, one or two characters, simple props.
- **Consistent characters.** A character sheet is made once and used as the reference for every frame.
- **One-pass narration.** The whole script is generated in a single ElevenLabs v3 request so the voice
  stays the same; readings are checked before generation; cut timing comes from per-character timestamps.
- **Cheap drafts, then finals.** Each model's allowed durations, resolutions and price are validated
  before submitting; `--dry-run` prints the estimate. Interrupted generations resume from a saved request id.
- **QA you can trust.** Every cut is reviewed as a 0.25-second contact sheet, plus automatic detection of
  frozen frames and harsh seams.
- **Auto-posting.** Approved posts are published at their scheduled time; local videos are handed to the
  Threads API through a private S3 bucket and a 1-hour signed URL.

## Install

**Claude Code**
```
/plugin marketplace add FinShinjuku/threads-video-studio
/plugin install threads-video-studio@threads-video-studio
```
A settings dialog opens for the API keys (secret fields are masked). Change them later with
`/plugin configure threads-video-studio@threads-video-studio`, then ask Claude to run `studio_save_settings` once.

**Claude Cowork / Claude Desktop (recommended)**
1. Download `threads-video-studio.mcpb` from the [latest release](https://github.com/FinShinjuku/threads-video-studio/releases/latest)
   and double-click it (or Customize → Connectors → Add → Install extension). A settings form opens:
   enter your fal.ai and ElevenLabs keys (and the posting settings if you post), then enable it.
   The keys stay inside the extension, which calls the APIs from your computer.
2. Add the skills: Customize → Plugins → Add → Add marketplace → `FinShinjuku/threads-video-studio`, then install.
3. Optional: connect your workspace folder (default `~/threads-videos`) to Cowork to browse the videos.

Before installing, set up the tools listed under Requirements below.
Without the extension, you can instead open `setup/setup.html`, save `config.json` and place it as
`tvs-config.json` in the folder connected to Cowork. That file holds your keys in plain text, so the
extension is the safer choice.

## 必要なもの（ご自身で入れてください） / Requirements

このプラグインは、下の道具を自動では入れません。使う前に、ご自身の Mac に入れてください。
The plugin does not install these for you. Install them on your Mac before first use.

| 道具 | 用途 | 入れ方（Homebrew） | 必須 |
|---|---|---|---|
| Python 3.10 以上 | すべてのスクリプトと拡張の実行（Mac 標準の 3.9 では動きません） | `brew install python` | 必須 |
| ffmpeg（ffprobe を含む） | 動画・音声の処理、コマ確認 | `brew install ffmpeg` | 必須 |
| Node.js 18 以上 | 字幕・BGM 入りの書き出し（HyperFrames を `npx` で実行） | `brew install node` | 必須 |
| pyopenjtalk | ナレーションの読みの事前チェック（無くても簡易チェックで動く） | `python3 -m pip install pyopenjtalk` | 任意 |
| AWS CLI | 動画を自動投稿するときの受け渡し（S3） | `brew install awscli` → `aws configure` | 自動投稿するなら |

Homebrew が無い場合は https://brew.sh の手順で先に入れてください。
入ったかどうかは、Claude に「studio_status を見て」と頼むか、`python3 scripts/setup.py status` で確認できます。

Accounts: fal.ai and ElevenLabs (required); Threads API and AWS (for posting). See [CONNECTORS.md](CONNECTORS.md).
- macOS is the tested platform. Scheduling uses launchd on macOS or a Cowork scheduled task.

## Skills

| Skill | When Claude uses it |
|---|---|
| `video-studio` | Entry point: decides which step you are at and follows the flow |
| `storyboard` | Plan, cut list, prompt self-check, approval packet |
| `character-sheet` | Character reference sheet, clean start frames, removing text/props |
| `generate-cuts` | Model choice, draft → final, QA, fixing broken cuts |
| `narration` | Reading check, one-pass narration, timing |
| `finish` | Signature BGM, captions, CTA, render, final QA |
| `threads-post` | Draft, approve, schedule, auto-publish, review |
| `studio-setup` | Keys, tools and connectors |

## Settings

| Key | Purpose |
|---|---|
| `fal_key` | fal.ai (images, video, music) |
| `elevenlabs_api_key`, `narrator_voice_id` | Narration and music |
| `threads_access_token`, `threads_user_id` | Posting and insights |
| `cta_url` | Link added to each post (a `?t=<slug>` tag is appended) |
| `media_bucket`, `aws_profile`, `aws_region` | Private S3 bucket for handing videos to Threads |
| `workspace` | Where projects, the posting queue and reports live (default `~/threads-videos`) |

Resolution order: plugin settings → environment variables → `tvs-config.json` in the current or workspace
folder → `~/.config/threads-video-studio/config.json` (mode 0600). Secrets are never printed.

## Privacy and safety

- Keys stay on your machine. `setup/setup.html` runs offline and sends nothing.
- Posting is irreversible on Threads, so the scripts only publish items that a person approved
  (`approved_by` is required), never retry an item left mid-publish, and never auto-approve.
- `threads_post.py approve --by <name> --note "<the human's OK, quoted>"` is an operating rule, not a
  technical check: Claude runs it only after a person has explicitly said OK in the chat, having seen the
  final video, text and time. The script cannot tell who is typing; the note is kept in `posts.json` for audit.
  Any later change to the text, topic, time or video (SHA-256) cancels the approval.
- Do not use real people's faces or names, third-party screens or logos without permission.

## License

MIT. Captions use Zen Maru Gothic (SIL Open Font License 1.1).
