---
name: studio-setup
description: "threads-video-studio の初期設定。fal.ai・ElevenLabs・Threads・S3 のキーと設定の入力方法を案内し、入力済みかを確認し、必要な道具（ffmpeg・Node・pyopenjtalk・AWS CLI）をそろえる。Use on first use, when a script says a setting is missing, or when the user asks how to enter API keys or connect services (\"初期設定\" \"APIキーを入れたい\" \"設定を確認\" \"setup\" \"configure keys\")."
---

# studio-setup — キーの入力と準備


## 1. キーを入れる（どれか1つ）
0. **Cowork／Claude デスクトップ（おすすめ）**：拡張 `threads-video-studio.mcpb` を入れる（GitHub の Releases からダウンロードしてダブルクリック、または カスタマイズ → コネクタ → 追加 → 拡張機能をインストール）。設定画面にキーを入れて有効にすれば、すぐ使える。キーは拡張の中だけで使われる。
1. **Claude Code のプラグイン設定画面（おすすめ）**：`/plugin` → Installed → threads-video-studio → Configure options。キーは伏せ字で入力でき、安全な保存先に入る。
   入力後、`studio_save_settings` ツールを1回呼ぶ（スクリプトが読む設定ファイルに写す）。
2. **設定画面（HTML）**：`open ${CLAUDE_PLUGIN_ROOT}/setup/setup.html` でブラウザに開き、入力して `config.json` を保存する。
   - Mac（Claude Code）：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup.py import ~/Downloads/config.json --delete`
   - Cowork：接続している作業フォルダの直下に `tvs-config.json` という名前で置く。
3. **ターミナル**：本人に `! python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup.py set fal_key` を実行してもらう（入力は画面に出ない）。

## 2. 確認
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup.py status
```
または `studio_status` ツール。値は伏せ字で表示される。

| 設定 | 必要になる場面 |
|---|---|
| `fal_key` | 画像・動画・BGM の生成（必須） |
| `elevenlabs_api_key`、`narrator_voice_id` | ナレーション（必須）、BGM |
| `threads_access_token` | 予約投稿・振り返り |
| `cta_url` | 投稿本文のリンク（例：公式 LINE） |
| `media_bucket`、`aws_profile`、`aws_region` | 動画の自動投稿（S3 経由） |

## 3. 道具
- `ffmpeg`（Mac：`brew install ffmpeg`）、Node.js 18 以上（HyperFrames の書き出し）
- `pip install pyopenjtalk`（読みの事前チェック。無くても簡易チェックで動く）
- 自動投稿をするなら AWS CLI と、非公開のバケット：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/media_host.py --create-bucket`（公開ブロック・暗号化・1日で自動削除）

## 4. 外部サービス
`${CLAUDE_PLUGIN_ROOT}/CONNECTORS.md` に、各サービスの用途・キーの取り方・料金の目安がある。
