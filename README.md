# threads-video-studio

Threads 向けの縦型 AI 動画（アニメ調・日本語ナレーション・字幕・BGM・誘導ボタン）を作り、予約投稿して振り返るための Claude プラグインです。Claude Code と Claude Cowork の両方で使えます。

## できること

```
企画 → 絵コンテ →【承認①】→ ナレーション → キャラ設定シート・起点画像
 → 下書き生成 → 全コマ確認 → 本番生成 → 字幕・BGM・誘導ボタン →【承認②】
 →【投稿の承認③】→ 予約時刻に自動投稿 → 48時間・7日の振り返り → 次の企画
```

- **人が決めるのは3か所だけ。** 絵コンテを承認するまでは、お金のかかる生成をしません。投稿の一式を承認するまでは、投稿しません。
- **短く作る。** 物語系は10〜15秒（3カット前後）、紹介系は20〜30秒。1カットに動作1つ・カメラの動き1つ、登場人物は1〜2人、小物は形の単純なものにします。
- **キャラの見た目をそろえる。** キャラ設定シートを1回作り、すべての絵の見本に使います。
- **ナレーションは1回で通して作る。** 原稿全体を ElevenLabs v3 で1回で生成するので、声色がそろいます。読みは生成前に確認し、カットの尺は文字ごとの時刻から決めます。
- **安い下書きから本番へ。** モデルごとに許される秒数・解像度・料金を送信前に確かめ、`--dry-run` で見積もりを出します。生成が途中で止まっても、保存した受付番号から続きを受け取ります。
- **流れを1枚の表で見せる。** 何秒から何秒まで、何を読み上げ、何を映すか、素材がどこまでできたかを1行1カットのキューシート（Excel）にします。人は「備考・ご指示」の列に書いて戻すだけで、指示が取り込まれます。
- **崩れを見落とさない。** すべてのカットを0.25秒ごとのコマ一覧で確認し、止まって見える区間やつなぎ目の乱れも自動で見つけます。
- **自動投稿。** 承認した投稿を予約時刻に公開します。手元の動画は、非公開の S3 に置いて1時間だけ有効な URL で Threads に渡します。

## インストール

**Claude Cowork／Claude デスクトップ（おすすめ）**
1. [最新のリリース](https://github.com/FinShinjuku/threads-video-studio/releases/latest) から `threads-video-studio.mcpb` をダウンロードして、ダブルクリックします（または、カスタマイズ → コネクタ → 追加 → 拡張機能をインストール）。
   設定画面が開くので、fal.ai と ElevenLabs のキーを入れます。投稿もするなら、投稿用の設定も入れます。入れたら有効にします。
   キーは拡張の中だけで使われ、あなたの PC から API を呼びます。
2. 手順（スキル）を入れます：カスタマイズ → プラグイン → 追加 → マーケットプレイスを追加 → `FinShinjuku/threads-video-studio` を入れてインストールします。
3. 任意：作業フォルダ（初期値 `~/threads-videos`）を Cowork に接続すると、できた動画を Cowork から見られます。

**Claude Code**
```
/plugin marketplace add FinShinjuku/threads-video-studio
/plugin install threads-video-studio@threads-video-studio
```
API キーの入力画面が開きます（秘密の項目は伏せ字）。あとから変えるときは `/plugin configure threads-video-studio@threads-video-studio` を実行し、そのあと Claude に `studio_save_settings` を1回実行してもらいます。

インストールの前に、下の「必要なもの」を入れておいてください。
拡張を使わない場合は、`setup/setup.html` をブラウザで開いて `config.json` を保存し、Cowork に接続したフォルダに `tvs-config.json` という名前で置く方法もあります。ただし、このファイルにはキーがそのまま入るので、拡張を使う方が安全です。

## 必要なもの（ご自身で入れてください）

このプラグインは、下の道具を自動では入れません。使う前に、ご自身の Mac に入れてください。

| 道具 | 用途 | 入れ方（Homebrew） | 必須 |
|---|---|---|---|
| Python 3.10 以上 | すべてのスクリプトと拡張の実行（Mac 標準の 3.9 では動きません） | `brew install python` | 必須 |
| ffmpeg（ffprobe を含む） | 動画・音声の処理、コマ確認 | `brew install ffmpeg` | 必須 |
| Node.js 18 以上 | 字幕・BGM 入りの書き出し（HyperFrames を `npx` で実行） | `brew install node` | 必須 |
| pyopenjtalk | ナレーションの読みの事前チェック（無くても簡易チェックで動く） | `python3 -m pip install pyopenjtalk` | 任意 |
| AWS CLI | 動画を自動投稿するときの受け渡し（S3） | `brew install awscli` → `aws configure` | 自動投稿するなら |

Homebrew が無い場合は、先に https://brew.sh の手順で入れてください。
入ったかどうかは、Claude に「studio_status を見て」と頼むか、`python3 scripts/setup.py status` で確かめられます。

## 必要なアカウント・契約（ご自身で用意してください）

このプラグインは、下のサービスのアカウントと契約を使います。どれも利用者ご本人の名義で作ってください。料金や条件は2026年10月時点のものなので、契約の前に各社のページで確かめてください。

| サービス | 何に使うか | 必要なアカウント・契約 | 必須 | 主な手順 |
|---|---|---|---|---|
| Claude（Anthropic） | プラグイン・拡張を動かす本体 | Claude の有料プラン（Pro・Max・Team など。Cowork と Claude Code が使えるもの） | 必須 | https://claude.ai でプランに入り、Claude デスクトップ（Cowork）か Claude Code を入れる |
| fal.ai | 画像・動画・BGM の生成 | fal.ai のアカウント。従量課金で、クレジットの購入（カード登録）が必要 | 必須 | https://fal.ai で登録 → クレジットを購入 → Auto top-up（自動チャージ）をオン → API キーを発行 |
| ElevenLabs | 日本語ナレーション、BGM | ElevenLabs のアカウント。動画を商用で使うなら有料プラン（Starter 以上）。BGM（Music）も有料プランで使う | 必須 | https://elevenlabs.io で登録 → プランを選ぶ → API キーを発行 → Voice Library から日本語の声を追加 |
| Threads | 投稿先 | Threads のアカウント（Instagram のアカウントでログインして作る） | 投稿するなら | Instagram アカウントで Threads に登録する |
| Meta for Developers | Threads API で予約投稿・反応の取り込み | Meta 開発者アカウントと、Threads の用途で作ったアプリ | 投稿するなら | https://developers.facebook.com で開発者登録 → アプリを作成（用途：Threads API にアクセス）→ 権限 `threads_basic`・`threads_content_publish`・`threads_manage_insights`・`threads_read_replies` を付ける → 自分の Threads アカウントをテスターに追加 → 長期アクセストークン（60日。延長可）を発行 |
| AWS | 動画を Threads に渡すための一時置き場（S3） | AWS アカウント（カード登録）と、S3 を使える IAM ユーザーまたはプロファイル | 動画を自動投稿するなら | https://aws.amazon.com で登録 → IAM ユーザーを作る → `aws configure` → `python3 scripts/media_host.py --create-bucket` |
| LINE 公式アカウント | 投稿から誘導する先（例） | LINE 公式アカウント（LINE Business ID）。誘導先は自社サイトなど任意の URL でもよい | 任意 | https://www.linebiz.com/jp/ で開設 → 友だち追加の URL を `cta_url` に入れる |

アカウントが要らないもの：Homebrew・Python・ffmpeg・Node.js・HyperFrames（`npx` で動く書き出しの道具）・字幕のフォント（Zen Maru Gothic）。

- 動画を作るだけなら、Claude・fal.ai・ElevenLabs の3つで足ります。
- 投稿まで自動にするなら、Threads・Meta for Developers・AWS も必要です。
- 発行したキーやトークンは、拡張の設定画面（またはプラグインの設定画面）にだけ入れてください。チャットに貼ったり、人に送ったりしないでください。

各サービスの設定項目との対応は [CONNECTORS.md](CONNECTORS.md) にあります。
動作を確かめているのは macOS です。定期実行は、macOS の launchd か Cowork のスケジュールタスクを使います。

## スキル（Claude への手順書）

| スキル | Claude が使う場面 |
|---|---|
| `video-studio` | 入口。依頼がどの段階かを判断して、流れに沿って進める |
| `storyboard` | 企画、カット表、プロンプトの自己採点、承認に出す資料 |
| `character-sheet` | キャラ設定シート、起点画像の清書、文字や小物を消す |
| `generate-cuts` | モデルの選び方、下書きから本番、確認、崩れたカットの直し方 |
| `narration` | 読みの事前チェック、ナレーションの通し生成、尺の確定 |
| `finish` | 使い回す BGM、字幕、誘導ボタン、書き出し、最終確認 |
| `threads-post` | 下書き、承認、予約、自動投稿、振り返り |
| `studio-setup` | キー・道具・接続の準備 |

## 拡張のツール（Cowork／デスクトップ）

拡張を入れると、Claude は次のツールを使えます。どれもあなたの PC の上で動き、作品は作品名（例：`t004-camp`）で指定します。

| ツール | 内容 |
|---|---|
| `studio_status`・`studio_paths`・`studio_save_settings` | 設定と道具の確認、保存先の確認、設定の保存 |
| `studio_new_project`・`studio_list_projects` | 作品フォルダの作成と一覧 |
| `studio_read_shots`・`studio_write_shots`・`studio_write_text` | カット表・企画書・ナレ原稿・読み替え辞書・投稿本文の読み書き |
| `studio_gen_image`・`studio_gen_video`・`studio_gen_voice`・`studio_gen_bgm` | 画像・動画・ナレーション・BGM の生成（有料。見積もりのみも可） |
| `studio_yomi_check`・`studio_qa` | 読みの事前チェック、コマの確認 |
| `studio_cuesheet` | キューシート（1行1カットで、何秒に何を読み・何を映し・素材がどこまでできたか）の書き出し・確認・人の指示の取り込み（無料） |
| `studio_assemble` | 字幕・BGM・誘導ボタンを入れて書き出す |
| `studio_post`・`studio_insights` | 投稿の下書き・承認・予約・公開、反応の取り込みと振り返り |
| `studio_job` | 時間のかかる処理の進み具合 |

## 設定

| 設定 | 用途 |
|---|---|
| `fal_key` | fal.ai（画像・動画・BGM） |
| `elevenlabs_api_key`、`narrator_voice_id` | ナレーションと BGM |
| `threads_access_token`、`threads_user_id` | 投稿と反応の取り込み |
| `cta_url` | 投稿本文に入れるリンク（`?t=<作品名>` が付く） |
| `media_bucket`、`aws_profile`、`aws_region` | 動画を Threads に渡すための非公開の S3 バケット |
| `workspace` | 作品・投稿の予定表・レポートを置く場所（初期値 `~/threads-videos`） |

設定を読む順番：拡張・プラグインの設定画面 → 環境変数 → 今いるフォルダか作業フォルダの `tvs-config.json` → `~/.config/threads-video-studio/config.json`（本人だけが読める権限）。秘密の値は画面に出しません。

## プライバシーと安全

- キーはあなたの PC から出ません。`setup/setup.html` はネットにつながずに動き、何も送信しません。
- Threads の投稿は取り消せません。そのため、スクリプトは人が承認したもの（`approved_by` が必須）だけを公開します。公開の途中で止まったものを自動でやり直したり、自分で承認したりはしません。
- `threads_post.py approve --by <名前> --note "<人の OK の言葉>"` は運用ルールで、技術的な判定ではありません。Claude は、人が完成動画・本文・時刻を見てチャットで明示的に OK と言ったあとにだけ実行します。スクリプトは誰が入力したかを判定できないので、その言葉を `posts.json` に記録して残します。承認のあとに本文・トピック・時刻・動画（SHA-256）のどれかが変わると、承認は取り消されます。
- 実在の人の顔や名前、他社の画面やロゴを、許可なく使わないでください。

## ライセンス

MIT。字幕のフォントは Zen Maru Gothic（SIL Open Font License 1.1）です。
