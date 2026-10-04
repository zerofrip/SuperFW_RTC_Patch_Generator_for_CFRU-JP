# SuperFW RTC `.sym` / `.patch` generator for CFRU-JP

Windows PowerShell 5.1とPython 3.8以降を使い、CFRU-JP適用済み日本語版FireRedからSuperFW用のRTCシンボルとパッチをオフラインで生成します。ROMの再構築ごとに生成してください。CFRU-JPベースでRTC関数に変更がなければ、他のハックROMでも動作します。

## 推奨: `.sym` と `.patch` をまとめて生成

Python 3.8以降をインストールし、`py -3` または `python` コマンドから起動できるようにしてください。実行時は `py -3` を先に確認し、利用できない場合は `python` を確認します。いずれもPython 3.8以降でなければ停止します。`SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.bat` に対象の `.gba` を1本だけドラッグ＆ドロップすると、ROMと同じフォルダーに同じベース名の `.sym` と `.patch` が生成されます。

```text
Pokemon_FireRed.gba
Pokemon_FireRed.sym
Pokemon_FireRed.patch
```

SuperFWでは、`.patch` を次のどちらかに配置してください。(1) ROMと同じフォルダーに、拡張子だけを `.patch` にした同じベース名で置く（例: `Pokemon_FireRed.gba` と `Pokemon_FireRed.patch`）。(2) SDカードの `/.superfw/patches/` に、同じベース名で置く（例: `/.superfw/patches/Pokemon_FireRed.patch`）。

SuperFW側ではROMを選択し、Patching optionsの `Patching` を `Patch engine`、`In-game menu` と `Emulated RTC` を `Enabled` にします。Loading optionsでRTC時刻を設定して `Remember config` で保存し、ROM informationからゲームを起動してください。IRQパッチがあればゲーム内メニューからRTC時刻を変更できます。

既存の `.sym` または `.patch` は上書きしません。両方を明示的に置き換える場合だけ、PowerShellから `-Force` を指定します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.ps1 -RomPath 'C:\path\to\Pokemon_FireRed.gba' -Force
```

## 従来のsym専用フロー

`.sym` だけが必要な場合は、従来どおり `SuperFW_RTC_Sym_Generator_for_CFRU-JP.bat` に `.gba` をドラッグ＆ドロップします。同じフォルダーに同名の `.sym` だけを生成します。このsym専用経路はWindows PowerShell 5.1だけで動作し、Pythonは不要です。PowerShell CLIも維持しています。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1 -RomPath 'C:\path\to\Pokemon_FireRed.gba' -Force
```

## 安全性とプライバシー

- ROMは読み取り専用で開き、内容を書き換えません。
- 拡張子、32 MiB上限、GBAヘッダーの `POKEMON FIRE` / `BPRJ` を検査します。
- RTC関数の署名が見つからない、重複する、解析に失敗する、またはpatch生成に失敗した場合は出力を公開しません。
- `.patch` はWAITCNT、Save、IRQ、RTC、ROM layoutの各情報を含み、SaveとIRQを有効にして公式Web版と同じ解析マージ順を適用します。RTCのアドレスは `.sym` のsymmap結果を優先します。
- ROM解析と生成はすべてローカルで行い、ネットワーク/APIには接続しません。ROMデータを送信・共有しません。
- 成功時にROM、`.sym`、`.patch` のSHA-256とパッチ件数を表示します。

署名は検証済み3 ROMで同一だったCFRU-JP RTC関数の完全な機械語です。コード配置が異なるROMにも対応しますが、CFRU-JPやコンパイラの別版で機械語が異なる場合は安全側に失敗します。

patch生成にはSuperFW公式patchtoolを同梱しています（`patchtool/`、GPL-3.0、`LICENSE`）。出典は[gba-patch-gen](https://github.com/davidgfnet/gba-patch-gen)のcommit `90131dcffdb2f56b84560011460ba619f9e128a5`です。

## テスト

すべて合成データを使い、実ROMは使用しません。

```powershell
pwsh -NoProfile -File .\tests\Test-SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1
python3 .\tests\test_generate_superfw_sym_patch.py
```
