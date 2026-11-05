# ZOUNOHANA2026 Go2 シミュレーション開発環境を Windows PC に構築する
#
# 入るもの:
#   Git for Windows / Python 3.12 / Claude Code / PyTorch (CUDA 12.8) / Isaac Sim 6.2 (pip 版)
#   プロジェクトフォルダ (CLAUDE.md と innu.maxpat をコピー)
#
# 使い方: このフォルダごと Windows PC にコピーし、管理者の PowerShell で
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -ProjectDir D:\go2sim
#
# 何度実行してもよい (入っているものは飛ばす)。所要: ダウンロード 20GB 前後, 30 分〜1 時間

param(
    [string]$ProjectDir = "C:\ZOUNOHANA2026",
    [switch]$SkipIsaac
)

$ErrorActionPreference = "Stop"
$IsaacVersion = "6.2.0.0"
$TorchVersion = "2.11.0"
$VenvDir = Join-Path $ProjectDir "env_isaacsim"

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin"
}

function Winget-Install($id) {
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
}

# ---------------------------------------------------------------------------
Step "0. 確認"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "管理者ではありません。長いパス名の設定だけ失敗する可能性があります。"
}
if (-not (Has winget)) {
    throw "winget がありません。Microsoft Store で「アプリ インストーラー」を更新してから再実行してください。"
}
if (Has nvidia-smi) {
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
} else {
    Write-Warning "nvidia-smi が見つかりません。Isaac Sim には NVIDIA ドライバが必要です。"
}

# ---------------------------------------------------------------------------
Step "1. 長いパス名を有効化 (pip のエラー防止)"
$fsKey = "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem"
if ((Get-ItemProperty $fsKey).LongPathsEnabled -ne 1) {
    if ($isAdmin) {
        New-ItemProperty -Path $fsKey -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force | Out-Null
        Write-Host "有効にしました"
    } else {
        throw "管理者の PowerShell で実行し直してください (LongPathsEnabled の設定に必要)。"
    }
} else {
    Write-Host "有効済み"
}

# ---------------------------------------------------------------------------
Step "2. Git for Windows (Claude Code が Bash を使うため)"
if (Has git) { git --version } else { Winget-Install "Git.Git"; Refresh-Path }

# ---------------------------------------------------------------------------
Step "3. Python 3.12 (Isaac Sim 6.2 は 3.12 のみ対応)"
$py312 = $null
if (Has py) { try { $py312 = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {} }
if (-not $py312) {
    Winget-Install "Python.Python.3.12"
    Refresh-Path
    $py312 = (& py -3.12 -c "import sys; print(sys.executable)")
}
Write-Host "Python 3.12: $py312"

# ---------------------------------------------------------------------------
Step "4. Claude Code"
Refresh-Path
if (Has claude) {
    claude --version
} else {
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
}
# インストーラーが PATH を通さないことがあるので、ユーザーの PATH に恒久的に追加する
$claudeBin = "$env:USERPROFILE\.local\bin"
if (-not (Test-Path "$claudeBin\claude.exe")) {
    throw "$claudeBin\claude.exe がありません。Claude Code のインストールに失敗しています。"
}
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $claudeBin) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$claudeBin", "User")
    Write-Host "PATH に追加: $claudeBin"
}
Refresh-Path
claude --version

# ---------------------------------------------------------------------------
Step "5. プロジェクトフォルダ: $ProjectDir"
New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null
foreach ($f in @("CLAUDE.md", "innu.maxpat")) {
    $src = Join-Path $PSScriptRoot $f
    $dst = Join-Path $ProjectDir $f
    if ((Test-Path $src) -and -not (Test-Path $dst)) {
        Copy-Item $src $dst
        Write-Host "コピー: $f"
    }
}
$gitignore = Join-Path $ProjectDir ".gitignore"
if (-not (Test-Path $gitignore)) {
    Set-Content -Path $gitignore -Encoding ascii -Value "env_isaacsim/`n__pycache__/`n*.log"
}
if (-not (Test-Path (Join-Path $ProjectDir ".git"))) {
    git -C $ProjectDir init | Out-Null
    Write-Host "git リポジトリを作成"
}

# ---------------------------------------------------------------------------
if ($SkipIsaac) {
    Write-Host "Isaac Sim のインストールを飛ばしました (-SkipIsaac)"
} else {
    Step "6. Python 仮想環境: $VenvDir"
    if (-not (Test-Path $VenvDir)) { & py -3.12 -m venv $VenvDir }
    $python = Join-Path $VenvDir "Scripts\python.exe"
    & $python -m pip install --upgrade pip

    Step "7. PyTorch $TorchVersion (CUDA 12.8)"
    & $python -m pip install "torch==$TorchVersion" --index-url https://download.pytorch.org/whl/cu128

    Step "8. Isaac Sim $IsaacVersion"
    & $python -m pip install "isaacsim[all,extscache]==$IsaacVersion" --extra-index-url https://pypi.nvidia.com

    Step "9. 互換性チェック (ウィンドウが開きます。閉じると続行)"
    $env:OMNI_KIT_ACCEPT_EULA = "YES"
    & (Join-Path $VenvDir "Scripts\isaacsim.exe") isaacsim.exp.compatibility_check
}

# ---------------------------------------------------------------------------
Step "完了"
Write-Host @"
次の手順:
  1. PowerShell を新しく開く (PATH を反映するため)
  2. cd $ProjectDir
  3. claude          ← 初回はブラウザでログイン
  4. Isaac Sim を試す:
       .\env_isaacsim\Scripts\activate
       isaacsim        ← Robotics Examples > POLICY > Go2 で歩くか確認

共用 PC なので、使い終わったら Claude Code 内で /logout してください。
"@
