# SuperFW RTC `.sym` generator for CFRU-JP

Windows標準のPowerShell 5.1だけで、CFRU-JP適用済み日本語版FireRedからSuperFW用RTCシンボルファイルを生成します

CFRU-JPベースでRTC関数に変更がなければ他のハックROMでも動作します

ROMの再構築ごとに生成が必要です


## 使い方

`SuperFW_RTC_Sym_Generator_for_CFRU-JP.bat` に対象の `.gba` をドラッグ＆ドロップしてください。同じフォルダーに同名の `.sym` が生成されます

```text
Pokemon_FireRed.gba
Pokemon_FireRed.sym
```

既存の `.sym` は上書きしません。明示的に上書きする場合だけPowerShellから `-Force` を付けます

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1 -RomPath 'C:\path\to\Pokemon_FireRed.gba' -Force
```

## `.sym`を`.patch`に適応する方法

SuperFWの公式サイトを参考にしてSave patchesとIRQ patches (for In-game menu)も一緒に適応してください

https://superfw.davidgf.net/docs/usermanual/patches/


## `.patch`の適応方法

1.  作成した`.patch`ファイルを`/.superfw/patches`にROMファイルと同名で保存

2.  起動後ROMを選択

3.  Patching options画面に移動

4.  PatchingをPatch engineに変更

5.  In-game menuをEnabledに変更

6.  Emulated RTCをEnabledに変更

7.  Loading options画面に移動

8.  RTC timeを変更(IRQ patchesを当てていればIn-game menuから変更できます)

9.  Remember configで設定を保存

10.  ROM information画面に移動してゲームを起動


## 安全性

- ROMは読み取り専用で開き、内容を書き換えません。
- 拡張子、32 MiB上限、GBAヘッダーの `POKEMON FIRE` / `BPRJ` を検査します
- RTC 4関数がそれぞれROM内に1件だけ見つかった場合に限り `.sym` を生成します
- 署名が見つからない、または重複した場合は何も生成せず終了します
- 成功時にROMと生成した `.sym` のSHA-256を表示します

署名は検証済み3 ROMで同一だったCFRU-JP RTC関数の完全な機械語です。コード配置が異なるROMにも対応しますが、CFRU-JPやコンパイラの別版で機械語が異なる場合は安全側に失敗します


## テスト

実ROMを使わない合成テストです

```powershell
pwsh -NoProfile -File .\tests\Test-SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1
```
