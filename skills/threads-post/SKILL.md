---
name: threads-post
description: "完成動画を Threads に予約投稿し、反応を振り返る。本文・リンク・予約時刻・トピックの一式を人に承認してもらい、承認済みだけを予約時刻に自動公開（動画は非公開ストレージの期限付き URL で渡す）し、48時間・7日で伸びたかを判定して次の企画につなげる。使う場面：Threads の投稿文を作る・承認をもらう・予約する・公開するとき、公開されたか確かめるとき、自動投稿の定期実行を設定するとき、反応を振り返るとき（「投稿して」「予約投稿」「Threads に出して」「公開された？」「振り返り」）。"
---

# threads-post — 承認・予約・自動公開・振り返り


**大原則：公開は取り消せない（Threads には編集も取り消しも無い）。承認済みにしてよいのは、人がチャットで投稿一式を見て、明示的に OK と言ったあとだけ。**

## 1. 下書き
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_post.py draft --project <dir> --text-file <dir>/post.txt --topic "AI活用" --at "2026-10-11 08:00"
```
- 本文は500字以内・リンク5個以下。設定に `cta_url` があれば、`?t=<slug>` を付けたリンクが本文の最後に自動で入る（どの投稿から来たか分かる）。
- 本文の型：つかみ1行 → 中身2〜3行 → 見る人への呼びかけ → リンク。名前・社名は入れない。

## 2. 承認（G3）
次の一式を**まとめて**見せる：①動画（Mac は `open -R final/<slug>-post.mp4`、それ以外はファイルの場所）②本文全文と文字数 ③リンク ④予約時刻 ⑤トピック ⑥狙い。
明示の OK が出たら：
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_post.py approve <slug> --by "<承認者の呼び名>" --note "<人がチャットで書いた承認の言葉>"
```
- スクリプトは、承認したのが人かどうかを技術的には判定できない。**このコマンドは、人の明示の OK を受けた後にだけ実行する運用ルール**。承認の言葉は記録に残る。
- 承認後に本文・動画・トピック・予約時刻のどれかが変わると、公開は止まる。変えたら承認し直す。

## 3. 自動公開
- `publish-due` が、承認済みで予約時刻を過ぎたものだけを公開する。動画は公開の直前に非公開の S3 に置き、1時間だけ有効な URL を Threads に渡す（中身は1日で消える）。
- **定期実行の設定（初回だけ）**
  - Mac（Claude Code）：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py`（確認）→ `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py --apply`（15分ごとに実行）
  - Cowork：`${CLAUDE_PLUGIN_ROOT}/templates/cowork-scheduled-task.md` の内容で、15分ごとのスケジュールタスクを作る。
- **この PC が起動していてネットにつながっていることが前提。** スリープ中やオフラインのときは公開されず、つながったあとの最初の実行で公開される。
- 今すぐ出したいとき：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_post.py publish-due --max 1`（予約時刻を過ぎていて承認済みのものだけ）。
- 状態の確認：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_post.py status`。`publishing` のまま止まったものは、Threads 上に投稿があるかを先に確認する（自動では再試行しない＝二重投稿の防止）。

## 4. 振り返り（毎朝）
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_insights.py pull     # 表示・いいね・返信・再投稿・引用・シェアを取り込む
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/threads_insights.py review   # 48時間・7日の時点で、伸びた/普通/伸びなかったを判定
```
- 判定は、過去の投稿の表示数の中央値との比（1.5倍以上で伸びた、0.67倍以下で伸びなかった）。過去が3本未満なら比較対象不足。
- `learnings.csv` の `insight`（なぜそうなったか、1行）と `next_action`（次に試すこと）を書く。Claude が書くときは、数字と返信を根拠に仮説として書き、断定しない。
- `next_action` を次の企画（`storyboard`）に渡す。
