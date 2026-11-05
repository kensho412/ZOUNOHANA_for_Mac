#!/usr/bin/env bash
# Mac → Go2 の拡張 PC（Jetson）へ Go2PathFollower を送る。
#
#   tools/deploy_go2.sh unitree@192.168.11.50            # ~/Go2PathFollower に送る
#   tools/deploy_go2.sh unitree@192.168.11.50 g2pf       # 送り先を指定（ホームからの相対パス）
#
# .venv・ログ・マーカー・キャリブレーションは送らない。送り先にしかないファイルは消さない。
# 初回は送ったあと、Jetson で tools/setup_go2_pc.sh を実行する（最後に表示されるコマンド）。
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "使い方: $0 <user@host> [送り先フォルダ（既定 Go2PathFollower）]" >&2
  exit 2
fi
TARGET="$1"
DEST="${2:-Go2PathFollower}"   # 相対パスはログイン先のホームから
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ssh -o ConnectTimeout=5 "$TARGET" "mkdir -p $DEST"
rsync -az \
  --exclude '.venv/' --exclude '.deps/' --exclude 'logs/' --exclude 'markers/' --exclude 'homography.json' \
  --exclude '__pycache__/' --exclude '.DS_Store' --exclude '*.pyc' \
  "$ROOT/" "$TARGET:$DEST/"

echo
echo "送りました: $TARGET:$DEST"
echo "初回だけ Jetson でセットアップ:"
echo "  ssh -t $TARGET 'cd $DEST && bash tools/setup_go2_pc.sh'"
