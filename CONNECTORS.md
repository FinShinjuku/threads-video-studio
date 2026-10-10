# 使う外部サービスと接続

| サービス | 用途 | 設定 | 必須 | 入手方法 |
|---|---|---|---|---|
| [fal.ai](https://fal.ai) | 画像（Seedream 4.5、Nano Banana Pro）、動画（Seedance 2.0、Kling 3.0。Veo 3.1 Fast は fal で 2026-10-22 に廃止予定）、BGM | `fal_key` | 必須 | ダッシュボード → Keys で発行。**Auto top-up（自動チャージ）をオンに**してください。残高切れでロックされている間に完成した生成結果は、あとから受け取れません。 |
| [ElevenLabs](https://elevenlabs.io) | 日本語ナレーション（`eleven_v3`、文字ごとの時刻つき）、BGM | `elevenlabs_api_key`、`narrator_voice_id` | 必須 | Settings → API keys で発行。Voice Library から日本語ネイティブの声を追加して、その ID を控えます。 |
| [Threads API](https://developers.facebook.com/docs/threads) | 予約投稿、反応の取り込み | `threads_access_token`、`threads_user_id`（任意） | 投稿するなら | Meta のアプリを Threads の用途で作り、長期トークンを発行します。`threads_post.py refresh-token` で延長できます。 |
| AWS S3 | 手元の動画を、1時間だけ有効な URL で Threads API に渡す | `media_bucket`、`aws_profile`、`aws_region` | 動画を自動投稿するなら | `python3 scripts/media_host.py --create-bucket` で、非公開・暗号化・1日で自動削除のバケットを作れます。 |
| HyperFrames（npm） | 字幕・誘導ボタン・音声を入れて MP4 に書き出す | — | 必須 | `npx` で動きます。Node.js 18 以上が必要です。 |

## 同梱の接続（MCP）

このプラグインには、ローカルで動く MCP サーバー `studio` が入っています。

- **Claude Cowork／Claude デスクトップ**：拡張（`threads-video-studio.mcpb`）として入れます。カスタマイズ → コネクタに設定画面が出て、そこに入れたキーは拡張の中だけで使われます。生成・組み立て・投稿のツール（`studio_*`）は、すべてあなたの PC の上で動きます。
- **Claude Code**：プラグインの `.mcp.json` から同じサーバーが動きます。設定はプラグインの設定画面に入れます。

ツールの一覧は [README.md](README.md) の「拡張のツール」にあります。

## 料金の目安（2026年10月。大量に作る前に各社のページで確認してください）

- 動画：Seedance 2.0 の 720p が1秒あたり約 $0.30（音声なしでも音声込みの料金）、Kling 3.0 は音声なしで1秒あたり約 $0.11。モデルごとの正確な単価は `scripts/gen_video.py` にあり、`--dry-run` で表示されます。
- 画像：Seedream 4.5 が1枚あたり約 $0.04。
- 15秒の物語（3〜5カット）で、1回の作り直しを含めて $5〜10 程度が目安です。
