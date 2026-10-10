---
name: generate-cuts
description: "起点画像からカットごとの動画を生成する。カットの性質でモデルを選び（安い下書き→本番の2段）、受付番号を保存して途中再開し、0.25秒ごとの全コマと数値チェックで崩れ・静止を見つけ、安い順に直す。Use when generating, regenerating or fixing video cuts from start frames, choosing a video model, estimating cost, or recovering an interrupted generation (\"動画を生成\" \"カットを作って\" \"作り直して\" \"崩れてる\" \"見積もり\" \"generate the cuts\")."
---

# generate-cuts — カットの生成・確認・直し方


## 1. モデルの選び方（全カットを同じモデルにしない）
| カットの性質 | 下書き | 本番 | メモ |
|---|---|---|---|
| アニメ調・人物1〜2人の普通の動き | `seedance20-fast`（480p） | `seedance20`（720p） | 標準。Seedance 2.0 は音声なしでも音声込みの料金になる |
| 同上で費用を抑えたい | — | `kling30` | 音声なしなら Seedance より安い。画風が保てるか下書きで比べる |
| 最初と最後の絵を決めたいカット | — | `seedance20`（`end_frame` を指定） | 終わりの絵を `end_frame` に置く |
| 情景（人物なし）・カメラワークが主役 | `seedance20-fast` | `seedance20-ref`（参照画像モード） | カメラの線を描いた参照画像を使う方法もある |
| 複数人物・大きな動き | — | `kling30` | 人物の動きと物理表現に強い |

`veo31-fast` / `veo31-fast-flf` は fal で 2026-10-22 に廃止予定のため、既定では使わない（使うと警告が出る）。
単価・許容秒数・解像度は改定が速いので、**生成前に必ず `--dry-run` で見積もる**（スクリプトが各モデルの許容値と単価を持っている）。
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_video.py --project <dir> --all --draft --dry-run   # 下書きの見積もり
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_video.py --project <dir> --all --dry-run           # 本番の見積もり
```

## 2. 生成（下書き→本番の2段）
1. **下書き**：`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_video.py --project <dir> --all --draft --variants 2`（安いモード・低解像度。1カット2本 → `clips/<cut>-draft-v1.mp4`, `-v2.mp4`）。
2. **確認**：下の QA を通す。不合格はプロンプトか起点画像を直して、下書きをもう一度。
3. **本番**：合格したカットだけ `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_video.py --project <dir> --cut c01`（本番画質）。
- 並列は最大5本。受付番号は投入直後に `clips/<cut>.task` に保存される。**通信が切れても、同じコマンドを再実行すれば続きから受け取る**（費用は二重にかからない）。
- 「残高切れでロック中に完成した結果は取得できない」と出たら、チャージしてから作り直す。fal は自動チャージ（Auto top-up）をオンにしておく。

## 3. QA（見せる前に必ず行う）
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/qa_frames.py --project <dir> --cut c01   # 0.25秒ごとのコンタクトシート＋数値チェック
```
コンタクトシートを開いて、全コマを次の観点で見る。
- 手・指の崩れ／**手に持つ物の形が変わっていないか**（増える・両端になる）
- 文字・吹き出しの残り／人数の増減／顔と服がカット間で同じか
- 主役がカメラの移動で画面の外に出ていないか
- 数値チェック：**0.4秒を超える静止**は WARN（止まった絵は死んで見える）。つなぎ目の差が大きすぎないか。

## 4. 直し方（安い順。最初から作り直さない）
1. **切る**：崩れた部分を尺から外す（仕上げで使う区間を短くする）。
2. **引き直し**：同じ条件でもう1回（結果は毎回変わる）。
3. **プロンプトを直す**：症状を「何秒の、どこが、どうなったか」で書き、その部分だけ指示を変える。
4. **起点画像を直す**：`character-sheet` の手直し（小物を消す・差し替える）→ 再生成。崩れの多くはここで直る。
5. **最初と最後の絵を固定**：`end_frame` に終わりの絵を置いて作り直す。
6. **カットを分ける**：動作を2つに分け、カットを増やす。

## 5. つなぎ方の選択肢
- **普通**：カットごとに起点画像から生成し、仕上げで切り替える。
- **切れ目なく1本に見せる**：前のカットの最後のコマを抜き出し（`ffmpeg -sseof -0.05 -i clips/c01.mp4 -frames:v 1 frames/c02.png`）、次のカットの起点にする。カメラが止まらない移動の演出に向く。
