# threads-video-studio — 実装の取り決め（内部向け）

このファイルは、スキル（Claude への手順書）とスクリプトの間の取り決めです。
スクリプトの引数や出力を変えたら、ここも合わせて直してください。

## 目的
Threads 向けに、アニメ調・イラスト調か顔を映さない実写寄りの、短い縦型（9:16）AI 動画を、日本語ナレーション・字幕・BGM・誘導ボタン付きで作り、予約して自動投稿し、結果を振り返る。お金のかかる生成の前と、投稿の前には、人の承認を必ず入れる。

## 動かす環境
- Python 3.10 以上、**標準ライブラリのみ**（任意で `yomi_check.py` 用の `pyopenjtalk`）。
- `ffmpeg`／`ffprobe` が PATH にあること。書き出しには `npx`（Node 18 以上）で HyperFrames を使う。S3 を使うときだけ `aws` CLI。
- どのスクリプトも：`python3 <plugin>/scripts/<name>.py --help` が動く。失敗したら1行の理由を出して0以外で終わる。秘密値を表示しない。もう一度実行しても壊れない（冪等か、続きから再開できる）。
- 設定は `scripts/common.py`（`common.get(key)`／`common.require(key)`）からだけ読む。キーの一覧は common.py の `SETTINGS`。

## 作品フォルダ（動画1本＝1フォルダ）
`<workspace>/<slug>/`（workspace の初期値は `~/threads-videos`。slug は例えば `t004-camp`。slug とカット ID は `^[A-Za-z0-9_-]{1,64}$` に限る）
```
brief.md                 # 言いたいこと、誰に向けるか、型（short|story|intro）、目標の尺
world.md                 # 任意。世界観設計（シリーズの正本 <workspace>/worlds/<シリーズ>.md の写し。skills/worldview）
shots.json               # カットの唯一の正本（下の形式）
storyboard/              # ラフ c01.png ...（どの生成でもよい）、確認用の index.html
character/               # sheet.png（キャラ設定シート）、refs/*.png
frames/                  # 本番の起点画像 c01.png ...（文字・吹き出し無し）、c01-end.png は任意
clips/                   # 生成したカット c01.mp4 と c01.json（受付番号・モデル・プロンプト・費用）
                         #   c01-draft.mp4 = 下書き（安いモード）。本番のカットは上書きしない
                         #   c01.task = 結果待ちの受付番号（再開用）
voice/                   # script.txt, yomi.json, all.mp3, align.json, lines.json
bgm/                     # bgm.mp3（選んだ曲）, candidates/
qa/                      # コマの一覧画像, qa.json
video/                   # HyperFrames のプロジェクト（assemble.py が作る）
final/                   # <slug>-final.mp4（1080x1920）, <slug>-post.mp4（投稿用に圧縮）
post.txt                 # 投稿本文（ツリーのフック＝1投稿目。動画を付けて自動公開する）
post-thread.txt          # 任意。ツリーの本文（2投稿目以降）。承認一式に含め、公開後に返信として出す
cuesheet.xlsx            # キューシート（cuesheet.py が shots.json から作る。人が備考・ご指示を書いて戻す）
```

## shots.json の形式
```json
{
  "slug": "t004-camp",
  "type": "story",                 // short（5〜10秒・1〜3カット）| story（10〜15秒・3カット前後）| intro（20〜30秒）
  "fps": 24,
  "cta": {"label": "LINEで話してみる", "sub": "プロフィールのリンクから", "show": true},
  "cuts": [
    {
      "id": "c01",
      "chapter": "つかみ",                       // 任意。章（まとまり）の名前
      "asset_id": "A1",                          // 任意。素材の目印（共有の素材を使い回すとき）
      "picture": "散らかったメモ帳に手が置かれる",  // 絵の内容（日本語）。人が読む用
      "kind": "ai",                              // ai（AI生成）| text（文字カット）| screen（画面収録）| footage（実写素材）
      "line": "友達5人で、海辺のキャンプへ！",       // ナレーションの1文（字幕の元にもなる）
      "caption": ["友達5人で、", "[[海辺のキャンプ]]へ！"], // 任意。字幕の行を手で決める。[[ ]] は強調する1語
      "action": "walk into the field",          // 被写体の動作を1つ
      "camera": "slow tracking forward",         // カメラの動きを1つ
      "prompt": "...",                           // 動画プロンプト（Claude が書き、人が承認する）
      "model": "seedance20",                     // gen_video の MODELS を参照
      "frame": "frames/c01.png",
      "end_frame": null,                         // 任意。最後のコマ（最初と最後を決めるモード）
      "refs": [],                                // 任意。参照画像（参照モード）
      "start": 0.0, "dur": 3.2,                  // ナレーションの工程で自動で入る
      "note": ""                                 // 任意。人からの備考・ご指示（キューシートから取り込む）
    }
  ]
}
```

## スクリプト（コマンドの取り決め）
パスは絶対パスか、`--project` で指定した作品フォルダからの相対パス。どちらも作品フォルダの外は指せない。

| スクリプト | 役割 | 主な引数 | 出力 |
|---|---|---|---|
| `setup.py` | 設定の確認・入力 | `status`／`set <key>`（入力は画面に出ない）／`path`／`import <json>` | 設定ファイル（0600） |
| `new_project.py` | 作品フォルダを作る | `<slug> --type short|story|intro` | フォルダ、brief.md、shots.json |
| `gen_image.py` | 画像の生成・編集（起点画像、キャラ設定シート、手直し） | `--prompt`、`--image`（参照、複数可）、`--model seedream45|nano-banana-pro`、`--out`、`--size 1440x2560` | png と横に `.json` |
| `gen_video.py` | カットごとの画像→動画。途中から再開できる | `--project`、`--cut c01` か `--all`、`--draft`（安いモード）、`--variants N`、`--model`、`--dry-run`（見積もりのみ）、`--with-draft` | `clips/<cut>.mp4/.json`、結果待ちの間は `.task` |
| `yomi_check.py` | 生成前の読みの確認 | `--project`（voice/script.txt を読む）、`--suggest` | 報告と `voice/yomi.json` への提案 |
| `gen_voice.py` | ElevenLabs v3 で原稿全体を1回で生成（文字ごとの時刻つき） | `--project`、`--voice-id`、`--tag "[cheerfully]"`、`--retime` | all.mp3、align.json、lines.json（各行の開始・終了）、shots.json の start/dur を更新 |
| `gen_bgm.py` | ボーカル無しの BGM | `--project`、`--prompt`、`--seconds`、`--provider elevenlabs|fal` | bgm/candidates/*.mp3 |
| `qa_frames.py` | コマの一覧画像と数値チェック | `--project`、`--cut`／`--all`／`--final`、`--fps 4` | qa/*.jpg、qa/qa.json（静止区間・動きの量・つなぎ目の差） |
| `cuesheet.py` | 1行1カットのキューシート（何秒に何を読み、何を映し、素材がどこまでできたか） | `--project`、`--out`（既定 cuesheet.xlsx）、`--check`（書き出さず確認だけ）、`--import <xlsx>` | cuesheet.xlsx（キューシート・この表の見方・確認の3シート）。import は shots.json の chapter・asset_id・picture・kind・note を更新 |
| `assemble.py` | shots.json・ナレ・BGM から HyperFrames を組んで書き出す | `--project`、`--render` | video/、final/<slug>-final.mp4、final/<slug>-post.mp4 |
| `threads_post.py` | 投稿の予定表と公開 | `draft`／`approve --by <名前> --note <承認の言葉>`／`schedule --at "YYYY-MM-DD HH:MM"`／`publish-due [--dry-run] [--max N]`／`status`／`mark-published <slug> --post-id`／`refresh-token`（どれも `--workspace` を付けられる。サブコマンドより前に書く） | `<workspace>/posts.json` |
| `threads_insights.py` | 反応の取り込みと振り返り | `pull`／`review` | `<workspace>/learnings.csv`、`<workspace>/reports/YYYY-MM-DD.md` |
| `media_host.py` | 手元の動画を非公開の S3 に置き、1時間有効の署名付き URL を作る | `<file>`／`--create-bucket [--dry-run]`／`--check`／`--show-url` | 既定では URL を表示しない |
| `install_scheduler.py` | macOS の launchd で `publish-due` を N 分ごとに動かす | 既定は確認のみ、`--apply`、`--uninstall`、`--interval` | `~/Library/LaunchAgents/com.threads-video-studio.publish.plist` |
| `threads_api.py` | Threads API の共通部品（トークン延長・原子的な書き込み・ロック） | — | — |

拡張（MCP サーバー `mcp/studio_server.py`）は、上のスクリプトを `studio_*` ツールとして包む。作品は slug で指定し、時間のかかる処理は裏で動かして `studio_job` で確認する。

### スクリプトに組み込んだ決まり
- gen_video：投入した直後に受付番号を `clips/<cut>.task` に保存し、再実行すると続きから受け取る。通信エラーでは再試行し、成功したら `.task` を消す。受け付けない値を黙って落とさない：モデルごとに秒数・解像度を確かめ、「Allowed: ...」で止める。`--dry-run` は見積もりを出す。結果の取得 URL は保存せず、エンドポイントと受付番号から作り直す。
- gen_voice：voice/script.txt を読む（空なら shots.json の `line`）。`--retime` は API を呼ばずに align.json から時刻だけ計算し直す。原稿全体を1回で生成する（`/v1/text-to-speech/{voice}/with-timestamps`、モデル `eleven_v3`、`language_code=ja`）。感情タグは冒頭に1つ。読み替え（`voice/yomi.json`）は発話にだけ使い、字幕は元の字面のまま。
- qa_frames：カットは1秒4コマ、完成版は1秒2コマで一覧にする。`freezedetect` で0.4秒を超える静止を警告する（止まった絵は死んで見える）。カットのつなぎ目の差の最大値を出す。
- cuesheet：標準ライブラリだけで xlsx を読み書きする（Excel で保存し直したものも読める）。状態は作品フォルダのファイルから決める（clips/<id>.mp4＝生成済、-draft.mp4＝下書きのみ、frames の画像だけ＝静止画のみ／文字カット、どれも無い＝未着手）。確認するのは、空欄・開始＋秒数と次の開始のずれ（0.05秒超）・1秒あたり6字超のナレ・型の目安の尺。import で shots.json に戻すのは計画の列（章・素材ID・絵の内容・素材の種類・備考）だけで、ナレ・字幕・プロンプトの書き換えは一覧で知らせるだけにする（声の作り直しと承認のやり直しにつながるため）。
- threads_post：承認のときに、本文・トピック・予約時刻・動画（SHA-256）のハッシュを保存する。どれかが承認のあとで変わったら、承認し直すまで公開しない。
- threads_post：公開するのは、status が approved で、approved_by があり、予約時刻を過ぎていて、published_id が無いものだけ。API を呼ぶ前に `publishing` にする。`publishing` のまま止まったものは自動でやり直さない。動画は `media_url` があればそれを使い、無ければ `video_file` を media_host で S3 に置く。トピックも付けられる（`topic`）。
