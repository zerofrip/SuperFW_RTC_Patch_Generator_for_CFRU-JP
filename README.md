# SuperFW RTC `.sym` / `.patch` generator for CFRU-JP

- Windows PowerShell 5.1とPython 3.8以降を使い、CFRU-JP適用済み日本語版FireRedからSuperFW用のRTCシンボルとパッチをオフラインで生成します

- ROMの再構築ごとに生成してください

## SuperFW v0.19-v0.21 のRTC互換性

SuperFW v0.19-v0.21 の公式 `SiiRtcGetDateTime` の日付変換には、年ループで年カウンターがインクリメントされない箇所があります。CFRU-JP は RTC データが検証を通らないと「日曜日 AM10:00」のダミー日時にフォールバックします。このジェネレーターは、年・月・日・曜日の変換を含む完全なハンドラーを置き換え、日付を1始まりにし、2000-01-01からの通算日に6を加えた曜日（日曜=0〜土曜=6）と24時間フラグ `0x40` を設定します。実機検証では、2026-10-06 09:05（火曜AM9:05）と2030-02-03 15:34（日曜PM3:34）の両方が正しく表示されることを確認しています。

この日付・曜日・status互換性修正の対象はSuperFW v0.19-v0.21です。`.patch` 形式にファームウェア版情報は含まれないため、他のバージョンでの動作は保証しません。修正を使うには、このジェネレーターで `.patch` を再生成してください。

gettimedateのROMオフセットが16 MiB以上の場合、混在Thumb/ARMの公式ハンドラーを直接使わず、layoutが示す空き領域の末尾1 KiBを予約して、修正済み0xC4バイト（49ワード）ハンドラーを配置します。元のentryは8バイトのThumb veneerに置き換えます。16 MiB未満のentryでは従来のインライン互換パッチを維持します。パッチ/veneer entry、ARM/Thumb遷移、UNDEF LRのタイムスタンプ、UNDEF SPの速度、時分秒のBIOS除算、IGM書き込みは実機ハードウェアで確認済みです。

- CFRU-JPベースでRTC関数に変更がなければ、他のハックROMでも動作します

## 推奨: `.sym` と `.patch` をまとめて生成

- Python 3.8以降をインストールし、`py -3` または `python` コマンドから起動できるようにしてください

- 実行時は `py -3` を先に確認し、利用できない場合は `python` を確認します

- いずれもPython 3.8以降でなければ停止します

`SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.bat` に対象の `.gba` を1本だけドラッグ＆ドロップすると、ROMと同じフォルダーに同じベース名の `.patch` が生成されます。`.sym` はパッチ生成中の一時ファイルで、成功後に削除されます

```text
Pokemon_FireRed.gba
Pokemon_FireRed.patch
```

SuperFWでは、`.patch` を次のどちらかに配置してください。

(1) ROMと同じフォルダーに、拡張子だけを `.patch` にした同じベース名で置く

（例: `/roms/Pokemon_FireRed.gba` と `/roms/Pokemon_FireRed.patch`）

(2) SDカードの `/.superfw/patches/` に、同じベース名で置く

（例: `/.superfw/patches/Pokemon_FireRed.patch`）

- SuperFW側ではROMを選択し、Patching optionsの `Patching` を `Patch engine`、`In-game menu` と `Emulated RTC` を `Enabled` にします

- Loading optionsでRTC時刻を設定して `Remember config` で保存し、ROM informationからゲームを起動してください

- IRQパッチがあればゲーム内メニューからRTC時刻を変更できます

- 既存の `.patch` は生成成功後に自動で上書きし、既存の `.sym` はパッチ公開後に削除します。生成または公開に失敗した場合は既存の `.sym` と `.patch` を維持・復元します

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.ps1 -RomPath 'C:\path\to\Pokemon_FireRed.gba'
```

## 従来のsym専用フロー

`.sym` だけが必要な場合は、従来どおり `SuperFW_RTC_Sym_Generator_for_CFRU-JP.bat` に `.gba` をドラッグ＆ドロップします

同じフォルダーに同名の `.sym` だけを生成します。このsym専用経路はWindows PowerShell 5.1だけで動作し、Pythonは不要です。PowerShell CLIも維持しています

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1 -RomPath 'C:\path\to\Pokemon_FireRed.gba' -Force
```

## 安全性とプライバシー

- ROMは読み取り専用で開き、内容を書き換えません

- 拡張子、32 MiB上限、GBAヘッダーの `POKEMON FIRE` / `BPRJ` を検査します

- RTC関数の署名が見つからない、重複する、解析に失敗する、またはpatch生成に失敗した場合は出力を公開しません

- `.patch` はWAITCNT、Save、IRQ、RTC、ROM layoutの各情報を含み、SaveとIRQを有効にして公式Web版と同じ解析マージ順を適用します

- RTCのアドレスは `.sym` のsymmap結果を優先します

- ROM解析と生成はすべてローカルで行い、ネットワーク/APIには接続しません

- ROMデータを送信・共有しません

- 成功時にROM、一時生成した`.sym`、`.patch` のSHA-256とパッチ件数を表示します。最終出力として残るのは`.patch`だけです

- 署名は検証済み3 ROMで同一だったCFRU-JP RTC関数の完全な機械語です。コード配置が異なるROMにも対応しますが、CFRU-JPやコンパイラの別版で機械語が異なる場合は安全側に失敗します

- patch生成にはSuperFW公式patchtoolを同梱しています（`patchtool/`、GPL-3.0、`LICENSE`）

- 出典は[gba-patch-gen](https://github.com/davidgfnet/gba-patch-gen)のcommit `90131dcffdb2f56b84560011460ba619f9e128a5`です。

## テスト

- すべて合成データを使い、実ROMは使用しません。

```powershell
pwsh -NoProfile -File .\tests\Test-SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1
python3 .\tests\test_generate_superfw_sym_patch.py
```
