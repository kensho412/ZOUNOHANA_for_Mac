#!/usr/bin/env bash
# Go2 の拡張 PC（Jetson, Ubuntu）で 1 回だけ実行する: go2pf 用の .venv と unitree_sdk2_python を用意する。
#
#   cd ~/Go2PathFollower && bash tools/setup_go2_pc.sh
#
# 何度実行してもよい（入っているものは飛ばす）。ロボットは動かさない。
# 手順は unitree_sdk2_python の公式 README（cyclonedds 0.10.x を自分でビルドして CYCLONEDDS_HOME を指定）に従う。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
# 借りている PC や共有の PC を汚さないよう、入れるものは全部このフォルダの .deps/ にまとめる
# （消すときは Go2PathFollower フォルダごと消せば元どおり）
SDK_DIR="${UNITREE_SDK_DIR:-$ROOT/.deps/unitree_sdk2_python}"
CY_DIR="${CYCLONEDDS_DIR:-$ROOT/.deps/cyclonedds}"
mkdir -p "$ROOT/.deps"

step() { echo; echo "==== $*"; }

step "Python: $($PY --version 2>&1)"
$PY -c 'import sys; assert sys.version_info >= (3, 8), "Python 3.8 以上が必要です"'

step ".venv を作る"
if [ ! -x .venv/bin/python ]; then
  if ! $PY -m venv .venv; then
    echo "venv を作れません。次を実行してからやり直してください: sudo apt install python3-venv" >&2
    exit 1
  fi
fi
VPY="$ROOT/.venv/bin/python"
"$VPY" -m pip install -q -U pip
"$VPY" -m pip install -q python-osc pyyaml numpy

if "$VPY" -c "import unitree_sdk2py, cyclonedds" 2>/dev/null; then
  step "unitree_sdk2py と cyclonedds は入っています"
else
  if ! "$VPY" -c "import cyclonedds" 2>/dev/null; then
    if [ -z "${CYCLONEDDS_HOME:-}" ]; then
      if [ ! -d "$CY_DIR/install/lib" ]; then
        step "cyclonedds 0.10.x をビルド（$CY_DIR、数分かかる）"
        command -v cmake >/dev/null || { echo "cmake がありません: sudo apt install cmake build-essential" >&2; exit 1; }
        [ -d "$CY_DIR" ] || git clone -b releases/0.10.x https://github.com/eclipse-cyclonedds/cyclonedds "$CY_DIR"
        mkdir -p "$CY_DIR/build" "$CY_DIR/install"
        (cd "$CY_DIR/build" && cmake .. -DCMAKE_INSTALL_PREFIX=../install -DBUILD_EXAMPLES=OFF \
          && cmake --build . --target install -j"$(nproc)")
      fi
      export CYCLONEDDS_HOME="$CY_DIR/install"
    fi
    step "cyclonedds 0.10.2（Python）を入れる  CYCLONEDDS_HOME=$CYCLONEDDS_HOME"
    "$VPY" -m pip install "cyclonedds==0.10.2"
  fi
  step "unitree_sdk2_python を入れる（$SDK_DIR）"
  [ -d "$SDK_DIR" ] || git clone https://github.com/unitreerobotics/unitree_sdk2_python.git "$SDK_DIR"
  # opencv-python は go2pf では使わないので入れない（--no-deps）
  "$VPY" -m pip install -e "$SDK_DIR" --no-deps
fi

# cyclonedds（Python）は実行のたびに CYCLONEDDS_HOME でライブラリの場所を知る必要がある。
# .venv の Python が起動するときに自動で設定されるよう、.pth を置く（TD から ssh で起動しても効く）
if [ -d "$CY_DIR/install/lib" ]; then
  SITE="$("$VPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  echo "import os; os.environ.setdefault('CYCLONEDDS_HOME', '$CY_DIR/install')" > "$SITE/go2pf_cyclonedds_home.pth"
fi

step "確認（CYCLONEDDS_HOME を外した状態で、実際の起動と同じ条件で読み込めるか）"
env -u CYCLONEDDS_HOME "$VPY" -c "import unitree_sdk2py, cyclonedds; from unitree_sdk2py.go2.sport.sport_client import SportClient; print('SDK OK')"
"$VPY" -m unittest tests.test_geometry tests.test_controller tests.test_supervisor tests.test_remote 2>&1 | tail -3

step "ネットワーク（192.168.123.x のアドレスを持つ NIC が、ロボット本体とつながっている。その名前を TD の Go2 の NIC 名に入れる）"
ip -br addr 2>/dev/null || ifconfig
echo
echo "完了。次は Mac のターミナルから、ロボットの向きの確認（tools/go2_check.py）を行う。"
