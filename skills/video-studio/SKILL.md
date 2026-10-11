---
name: video-studio
description: "Threads 向け縦型 AI 動画づくりの入口（オーケストレータ）。企画→絵コンテ→承認→ナレ→キャラ設定シート→起点画像→下書き生成→QA→本番生成→仕上げ→承認→予約投稿→振り返りを、どの段階かを判定して該当スキルへ振り分ける。使う場面：Threads・Reels・TikTok 向けの短い縦型 AI 動画を作る・企画する・直す・投稿するとき（「動画を作って」「Threads用の動画」「ショート動画」「絵コンテ」「このカットを直して」「投稿して」）。"
---

# video-studio — Threads 動画づくりの入口

この動画づくりは、**人が決めるところ（承認ゲート）を3回だけ置き、残りは Claude が回す**作り方です。
まず、依頼がどの段階にあたるかを判定し、下の表の該当スキルを読んでから進めます。迷ったら Step 1 から始めます。

初回、または設定が分からないときは、先に `studio-setup` スキルを読みます。

## 実行のしかた（2通り）
- **拡張（Threads Video Studio）が入っているとき（Cowork はこちら）**：`studio_*` ツールを使う。キーは拡張の設定画面に入っていて、ツールはこの PC の上で API を呼ぶ。プロジェクトは作品名（slug）で指定する。時間のかかる生成・書き出し・投稿は裏で動くので、`studio_job` で進み具合を見る。
  - 対応表：`new_project.py`→`studio_new_project`、`gen_image.py`→`studio_gen_image`、`gen_video.py`→`studio_gen_video`、`yomi_check.py`→`studio_yomi_check`、`gen_voice.py`→`studio_gen_voice`、`gen_bgm.py`→`studio_gen_bgm`、`qa_frames.py`→`studio_qa`、`cuesheet.py`→`studio_cuesheet`、`assemble.py`→`studio_assemble`、`threads_post.py`→`studio_post`、`threads_insights.py`→`studio_insights`。shots.json・原稿・本文の書き込みは `studio_write_shots`・`studio_write_text`。
- **拡張が無いとき（Claude Code）**：各スキルに書いた `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/...` を Bash で実行する。
- どちらが使えるか分からないときは、まず `studio_status` を呼ぶ。

## 工程とスキル

| # | 工程 | 担当 | スキル |
|---|---|---|---|
| 1 | 企画（言いたいこと1文・型・誰に） | Claude が案、人が選ぶ | `storyboard` |
| 2 | 絵コンテ（カット表＋ラフ画像）→ キューシート（1行1カットの表）→ プロンプトの自己採点 | Claude | `storyboard` |
| **G1** | **絵コンテ・ナレ原稿・見積もりの承認** | **人** | — |
| 3 | 読みの事前チェック → ナレを通しで1回生成 → 尺を確定 | Claude | `narration` |
| 4 | キャラ設定シート（初回だけ）→ 起点画像を清書・仮置きを消す | Claude | `character-sheet` |
| 5 | 下書き生成（安いモード）→ 0.25秒 QA → 合格カットだけ本番 | Claude | `generate-cuts` |
| 6 | BGM・字幕・CTA で仕上げ → 0.5秒 QA | Claude | `finish` |
| **G2** | **完成動画の承認** | **人** | — |
| **G3** | **投稿一式（動画・本文・リンク・予約時刻）の承認** | **人** | `threads-post` |
| 7 | 予約時刻に自動公開 | 自動 | `threads-post` |
| 8 | 48時間・7日の振り返り → 次の企画 | 自動＋人 | `threads-post` |

## 守るルール
1. **承認前にお金のかかる生成をしない。** G1 の前に行ってよいのは、見積もり（`gen_video.py --dry-run --with-draft`、無料）と、枚数と金額を伝えて了承を得たラフ画像だけ。曖昧な返事（「いいね」「たぶん」）は承認として扱わない。
2. **尺は型で決める。** 物語系は10〜15秒・3カット前後、紹介系は20〜30秒。短くするのは品質と費用のため（崩れ・確認の手間・作り直しは、カット数に比例して増える）。良し悪しは完走率・送信・リンクのクリックで判断する。
3. **1カット＝被写体の動作1つ＋カメラの動き1つ。** 登場人物は1〜2人まで。手に持つ小物は形の単純なものにする（刃物や細い道具は崩れやすい）。
4. **人物はアニメ調・イラスト調で作る。** 実写の人物は不気味に見えやすいので、使うのは本人が明示したときだけ。
5. **ナレは原稿全体を1回で通して生成し、声色をそろえる。** 行ごとに別々に作らない。原稿は1秒あたり5〜6字に収める。
6. **起点画像に、文字・吹き出し・ボタンを残さない。** 文字は仕上げで載せる。
7. **崩れは自分で先に見つける。** 生成したカットは 0.25秒ごとの全コマで確認してから見せる。数コマの抜き取りだけで見せない。
8. **名前・ロゴ・実在の顔・第三者の画面は、本人の許可なしに入れない。**
9. **止まっても続きから再開する。** 生成の受付番号は保存され、再実行すると続きから受け取る。お金をかけて最初から作り直さない。

## 1本の目安（2026年10月の実績）
- 物語系15秒・5カット（Seedance 2.5）：約750円。紹介系42秒・7カット（Seedance 2.0）：約2,800円。このうち作り直しで約1,300円失った。
- 見積もりは `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_video.py --project <dir> --all --dry-run --with-draft` で毎回出し、G1 の承認に含める。

## プロジェクトの作り方
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/new_project.py t004-example --type story   # ~/threads-videos/t004-example/ ができる
```
`shots.json` が、カット・プロンプト・尺の唯一の正本です。絵コンテ・生成・仕上げ・投稿は、すべてここから読みます。
人に見せるときは、shots.json をそのまま見せず、キューシート（`cuesheet.py` で作る `cuesheet.xlsx`）にして見せます。工程が進むたびに書き出し直し、状態の列で進み具合が分かるようにします。
