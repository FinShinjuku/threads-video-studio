---
name: character-sheet
description: "キャラクターの見た目をカット間でそろえるための設定シート作りと、動画の起点画像（最初のコマ）の清書・手直し。ラフを参照に高解像度で描き直し、吹き出し・文字・崩れやすい小物を消す。Use when creating a character reference sheet, keeping a character consistent across cuts, making clean start frames from rough storyboards, or removing text/bubbles/props from a frame (\"キャラ設定\" \"見た目をそろえる\" \"起点画像\" \"吹き出しを消して\" \"character sheet\")."
---

# character-sheet — 見た目をそろえる・起点画像を清書する


## 1. キャラ設定シート（キャラごとに1回だけ作り、使い回す）
1. **キービジュアル**を1枚作る（全身・正面寄り・無地背景、9:16）。人が見て OK した絵を正本にする。
2. それを参照に、**設定シート**を1枚作る：正面・斜め・横・後ろ、表情3種（笑顔・驚き・困り顔）、手に持つ物。
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_image.py --project <dir> --model nano-banana-pro --image character/key.png --size 2560x1440 \
     --prompt "Character reference sheet of the SAME character as the reference image: front, three-quarter, side and back views, plus three expressions (smile, surprised, troubled). Same outfit, colors and proportions. Plain light background, no text, no labels." \
     --out character/sheet.png
   ```
3. **見た目の説明文**を1つに固定し、`brief.md` に書く（例：「wavy dark-brown hair, round face, rosy cheeks, grey blazer over white T-shirt, grey trousers」）。以後、すべての画像・動画のプロンプトに**同じ文言**で入れる。
4. 設定シートは**参照用**。動画の起点にはしない（起点は、実際の場面の絵にする）。

## 2. 起点画像の清書（承認後・カットごと）
ラフ（storyboard/）は構図の確認用です。本番の起点は、設定シートとラフを参照に、高解像度で描き直します。
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_image.py --project <dir> --model nano-banana-pro --size 1440x2560 \
  --image character/sheet.png --image storyboard/c01.png \
  --prompt "<画風1文>. Recreate the composition of the second reference image with the character exactly as in the first reference sheet. <見た目の固定文>. <場面>. No text, no speech bubbles, no logos." \
  --out frames/c01.png
```
- 出力は **9:16（1440×2560）**。動画モデルは起点画像の比率に従うものが多い。
- 文字・吹き出し・ボタンの位置取りがラフに描いてあっても、**起点画像では消す**（文字は仕上げで載せる）。

## 3. 手直し（部分だけ直す）
崩れやすい小物の差し替えや、仮置きの削除は、描き直しではなく編集で行う。
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gen_image.py --project <dir> --model seedream45 --image frames/c05.png \
  --prompt "Remove the sickle: his right hand is empty, raised and waving (open palm, five fingers). Keep everything else exactly the same." \
  --out frames/c05-fix.png
```
確認して良ければ `frames/c05-fix.png` を `frames/c05.png` に置き換える（元の絵は `frames/c05-orig.png` に残す）。
- 「消す」「置き換える」だけに使う。画風を変える指示は崩れの元になる。
- 直した絵は、必ず元の絵と並べて目で確認してから使う。

## チェック
- [ ] キャラの見た目の固定文が、全カットのプロンプトに同じ文言で入っている
- [ ] 起点画像が 9:16・高解像度で、文字・吹き出し・ロゴが無い
- [ ] 手に持つ物が形の単純なもの
