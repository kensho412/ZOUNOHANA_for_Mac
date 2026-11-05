# Windows PC 環境構築キット

研究室の Windows PC に、Go2 シミュレーションの開発環境を一発で作る。

| ファイル | 内容 |
|---|---|
| `setup_windows.ps1` | 環境構築スクリプト |
| `CLAUDE.md` | Windows 側の Claude Code に渡すプロジェクトの前提 (パッチの OSC 形式、Isaac Sim 6.2 の API メモ) |
| `innu.maxpat` | 現在の Max パッチのコピー (Claude が形式を確認するため) |

## 入るもの
- Git for Windows
- Python 3.12
- Claude Code
- PyTorch 2.11 (CUDA 12.8)
- Isaac Sim 6.2 (pip 版、プロジェクト内の `env_isaacsim\` に入る)
- プロジェクトフォルダ `C:\ZOUNOHANA2026` (CLAUDE.md・innu.maxpat をコピー、git 初期化)

## 手順
1. この `isaac_sim` フォルダを USB などで Windows PC にコピー
2. スタートメニューで PowerShell を右クリック → **管理者として実行**
3. コピーしたフォルダに移動して実行:
   ```powershell
   cd C:\Users\<ユーザー名>\Desktop\isaac_sim
   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
   ```
   - フォルダの場所を変える: `-ProjectDir D:\go2sim`
   - Claude Code と Git だけ入れる: `-SkipIsaac`
4. 最後に Isaac Sim の互換性チェック画面が出る。GPU とドライバが緑なら OK
5. **PowerShell を開き直して**:
   ```powershell
   cd C:\ZOUNOHANA2026
   claude
   ```
   初回はブラウザでログイン (Pro / Max プランのアカウント)

## 注意
- ダウンロードは 20GB 前後、30 分〜1 時間かかる
- 途中で失敗しても、もう一度実行すれば続きから進む
- 管理者として実行するのは、長いパス名の設定に必要なため。普段使うのと**同じ Windows アカウント**で管理者実行すること (別アカウントだと Claude Code がそちらに入る)
- 共用 PC なので、使い終わったら Claude Code 内で `/logout`
