# Go2 経路追従コントローラ（go2pf）

天井カメラ＋ArUco で TouchDesigner が検出したロボット位置（OSC `/dog/pose`）を受け取り、
JSON で与えたルートを Pure Pursuit で追従するように、Unitree Go2 へ速度指令（`SportClient.Move`）を送る。

```
TouchDesigner ──OSC /dog/pose (x, y, yaw, found) 30Hz──▶ go2pf ──SportClient.Move(vx, vy, vyaw)──▶ Go2
                                                         │
                                                  ルート JSON / YAML 設定
```

> **ソフトウェアの非常停止（スペースキー）は安全規格上の非常停止ではありません。**
> 実機で動かすときは必ず Go2 のリモコンを手に持ち、ソフトが止まらない場合はリモコンで止めてください。

---

## 構成

```
go2pf/
  geometry.py    wrap・目標方向・座標変換・折れ線（最近点探索）・ジオフェンス
  controller.py  Pure Pursuit と加速度制限（RateLimiter）
  supervisor.py  状態管理と安全監視。ロボットへの指令はすべてここを通る
  backends.py    RobotBackend / Go2Backend（実機）/ MockBackend + MockWorld + MockPoseFeeder
  pose.py        OSC 受信、最新位置のバッファ
  config.py      YAML 設定の読み込みと検証、ルート JSON の読み込み
  csvlog.py      CSV ログ
  viz.py         matplotlib 可視化
  keyboard.py    ターミナルの 1 文字キー入力
  remote.py      TouchDesigner からの操作（/go2pf/config, arm, estop, quit）と状態の送り返し
  main.py        起動・スレッド構成・終了処理
config/default.yaml     設定例（全パラメータ）
config/mock_noisy.yaml  Mock の負荷試験用（ノイズ・遅延・見失いを強める）
routes/example_route.json  U 字ルート（段差区間だけ遅くする例）
routes/figure8.json        8 の字（自己交差あり、--loop 用）
touchdesigner/build_td.py    TD の Textport で実行すると、操作用の COMP「go2」を一式作る
touchdesigner/go2ctl.py      go2 の中身（検出・送信・起動/終了・アーム・非常停止・状態表示・描画）
touchdesigner/aruco_pose.py  ArUco 検出 → 部屋座標の x, y, yaw（go2ctl から使う。単体でも動く）
tools/make_markers.py   印刷用マーカー画像の生成（1 枚ずつ PNG）
tools/make_marker_sheet.py  マーカーを紙にまとめた印刷用 PDF（A4 / A3 / B4 / Letter）
tools/deploy_go2.sh     Mac → Go2 の拡張 PC（Jetson）へソースを送る
tools/setup_go2_pc.sh   Jetson で 1 回だけ: .venv と unitree_sdk2_python（cyclonedds）を用意
tools/osc_monitor.py    TouchDesigner からの OSC を表示するだけ（ロボットに何も送らない）
tools/go2_check.py      実機で一度だけ行う、符号と指令途絶時の挙動の確認
tests/                  テスト（go2pf 本体は外部依存なし。TD 側は TD の偽物で go2ctl を動かす）
```

## セットアップ

Python 3.8 以上。

```bash
cd Go2PathFollower
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

テスト（依存パッケージなしで動く。OpenCV が入っていれば ArUco のテストも走る）:

```bash
python3 -m unittest discover -s tests -t .
```

### 実機のセットアップ（Go2 を動かす PC のみ）

[unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python) を公式の手順で入れる。
`cyclonedds==0.10.2` が必要で、見つからないと言われたら公式 README の FAQ に従って cyclonedds をビルドし `CYCLONEDDS_HOME` を設定する。

```bash
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
pip install -e .
```

ネットワークは Unitree の [Quick start](https://support.unitree.com/home/en/developer/Quick_start) に従い、
ロボットと有線でつないだ NIC（ここでは `eth0`）にロボット側と同じサブネットの固定 IP を設定する。
この PC は同時に TouchDesigner からの OSC（UDP 9000）も受けるので、ファイアウォールで UDP 9000 を開けておく。

## Mock で確認する（実機なし）

```bash
# 可視化つき。ウィンドウかターミナルで a を押すとアーム、space で非常停止、q で終了
.venv/bin/python -m go2pf --route routes/example_route.json

# ノイズ・遅延・見失いを強めて、OSC 受信経路ごと確認（シミュレーション位置を自分の 9000 番へ OSC で送る）
.venv/bin/python -m go2pf --config config/mock_noisy.yaml --route routes/example_route.json --mock-osc

# 8 の字をループ走行
.venv/bin/python -m go2pf --route routes/figure8.json --loop

# 画面なしで 40 秒だけ自動実行（CI やログ確認用。--auto-arm は mock 専用）
.venv/bin/python -m go2pf --route routes/example_route.json --no-viz --auto-arm --duration 40
```

`MockBackend` は速度指令を一次遅れ（`mock.velocity_tau`）で追従して積分し、その位置にノイズ・遅延・見失いを加えて
OSC 入力の代わりに使う。可視化には、ルート（灰）、ジオフェンス（赤破線）、現在位置と向き（色は状態）、
目標点（紫の ×）、軌跡、指令値が出る。

## TouchDesigner から全部操作する

起動・設定・調整・アーム・非常停止・終了まで、すべて TouchDesigner の COMP「go2」のパラメータで行う。
go2pf は TD から子プロセスとして起動され（または別 PC で起動し）、TD とは OSC だけでやりとりする。

```
 TouchDesigner（COMP go2）                                     go2pf（別プロセス）
 ┌──────────────────────────────┐   /dog/pose  x y yaw found  ┌───────────────────────┐
 │ カメラ → ArUco → 部屋座標    │ ──────────────────────────▶ │ Pure Pursuit          │
 │ パラメータ・ルート表         │   /go2pf/config（1 秒ごと）  │ 安全監視              │──▶ Go2
 │ 起動/終了・アーム・非常停止  │   /go2pf/arm /estop /quit    │                       │
 │ 俯瞰図・カメラ重ね描き       │ ◀────────────────────────── │                       │
 └──────────────────────────────┘   /go2pf/status pose cmd ack └───────────────────────┘
            UDP 9000 →  /  ← UDP 9001
```

### 1. 準備（初回のみ）

1. **Python 環境**（go2pf 用。TD の Python とは別）
   ```bash
   cd /Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt opencv-contrib-python
   ```
   フォルダを移動したら `.venv` は作り直す（中のパスが古いまま残るため）。
2. **マーカーを印刷**：1 枚ずつの PNG なら `.venv/bin/python tools/make_markers.py --size-cm 18 --out markers`、
   **紙にまとめた PDF** なら
   ```bash
   .venv/bin/python tools/make_marker_sheet.py --paper A4              # A4 1 枚に全部（6.0 cm。机の上での確認用）
   .venv/bin/python tools/make_marker_sheet.py --paper A3              # A3 1 枚に全部（9.1 cm）
   .venv/bin/python tools/make_marker_sheet.py --paper A3 --size-cm 15 # 本番用の大きさ（入りきらなければ複数ページ）
   ```
   出力は `markers/marker_sheet.pdf`（`--out` で変更）。**「実際のサイズ」「100%」で印刷**し、下の 10 cm のものさしを定規で確認する。
   点線で切り分ける（点線の内側の白い余白は検出に必要なので切らない）。マーカーの大きさは位置の計算には使わないので、
   机の上の確認なら小さくてもよい。本番の天井カメラからは 12〜18 cm を目安に（ロボット用は背中に収まる大きさで）。
   - `aruco_0〜3`：床の基準マーカー（座標の基準。区画の四隅付近に貼る）
   - `aruco_10`：ロボットの背中。**上辺（ラベル側）を機体の頭の方へ**向けて、平らに貼る
   - 白い余白は切らない。つや消しの紙に印刷する
3. **TD に go2 を作る**：TouchDesigner 2025 で新しいプロジェクトを開き、Textport（Dialogs > Textport and DATs）に貼って Enter
   ```python
   exec(open('/Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower/touchdesigner/build_td.py', encoding='utf-8').read())
   ```
   `/project1/go2` ができる。中身：`camera`（Video Device In）→ `mono` → `cam`、`route`（ルートの表）、
   `go2ctl`（本体。go2ctl.py と同期）、`frame` / `pars` / `keys`（コールバック）、`map` / `overlay`（表示用 Script TOP）、`route_shape` → `route_sop`（ルートの形）。
   何度実行してもよい（既存の値は消えず、足りないものだけ足す）。`[注意]` が出たら下の「TD 側の未確認点」を参照。
4. `go2/camera` で天井カメラのデバイスを選ぶ。プロジェクトを保存する（`homography.json` はプロジェクトと同じ場所ではなく Go2PathFollower フォルダに保存される）。

TD 2025（このマシンの 2025.30060 / 2025.33230）には OpenCV 4.11（ArUco 入り）と numpy が同梱されているので、TD 側に追加のインストールは不要。

### 2. go2 のパラメータ

| ページ | 主な項目 |
|---|---|
| **Run** | `go2pf を起動` / `go2pf を終了` / `アーム` / `非常停止 [space]`、バックエンド（Mock / Dry / Go2）、ループ、ルート JSON の読み込み・保存、TD 起動時に自動起動 |
| **Route** | ルートの元（表 / SOP）、参照する SOP、SOP の平面、閉じた形ならループ、逆向き、始点をずらす、点の間隔 |
| **Connect** | go2pf の IP とポート（既定 127.0.0.1:9000、状態の受信 9001）、フォルダ、Python、Go2 の NIC 名、起動コマンドの上書き |
| **Camera** | 検出 On/Off、カメラ TOP、送信周波数、**カメラの高さ**、**マーカーの高さ**、カメラ真下（自動 / 手入力）、向きの補正、ロボットの ID、辞書、キャリブレーションのやり直し |
| **Area** | **基準マーカー 4 枚の位置**（＝座標の決め方）、**ジオフェンスの最小・最大**、外へ向かう予測時間、俯瞰図の余白 |
| **Control** | 制御周期、先読み距離、速度、上限、旋回ゲイン、加速度制限、到達判定、最近点探索の範囲 |
| **Safety** | 見失いタイムアウト、見失いから自動で再開するか |
| **Status** | go2pf プロセス、状態と理由、設定の反映（OK / エラー内容）、キャリブレーション、検出率と揺れ、位置、指令 |

- Control / Safety / Area（フェンス）とルートの表は、**走行中でも変えた瞬間に go2pf へ送られる**（さらに 1 秒ごとに再送）。
  go2pf 側で検証され、範囲外の値・フェンスがルートを含まない変更などは**丸ごと不採用**になり、`Status > 設定の反映` に理由が出る。
- ルートの表（`go2/route`）とループの切り替えは、**走行中は反映されない**（止めてから反映される）。
- 速度の上限は TD のスライダーでも go2pf 側でも 1.0 m/s を超えられない。
- 起動コマンド・ポート・バックエンド・NIC 名を変えたときは、go2pf を一度終了して起動し直す。

### ルートを SOP で作る（リアルタイム反映）

`Route > ルートの元` を **SOP** にすると、`Route > ルートの SOP` で参照している SOP の形がそのままルートになる。
build_td.py は例として `go2/route_shape`（Circle SOP、半径 1.5 m）→ `go2/route_sop`（Null SOP）を作り、`route_sop` を参照する。
**`route_sop` の手前を好きな SOP（Line / Rectangle / Add / Noise / Transform …）に差し替える**か、
別の場所に作った Null SOP をパラメータにドラッグして参照先を変える。

- **単位はメートル、座標は部屋座標**（原点が区画の中心）。大きさと位置は SOP 側（Transform SOP など）で合わせる。
- 使う平面は `SOP の平面` で選ぶ：XY（既定。Circle / Rectangle SOP の既定の向き）、XZ、XZ（z を反転）。
- 点の順番は **最初のプリミティブの頂点の順**（プリミティブがなければ点の番号順）。これが進む向きになる。
  逆に走りたいときは `逆向きに走る`、ループの始点を変えたいときは `始点をずらす`（0〜1）。
- **閉じた形（閉じたポリゴン、または最初と最後が同じ点）は自動でループ走行**になる（`閉じた形ならループ`）。
  閉じているかが分からない SOP では `Run > ループ走行` に従う。
- 点アトリビュート **`speed`** があれば、その点から次の点までの区間速度として使う（Point SOP / Attribute Create SOP などで付ける）。
- 点が細かすぎる・粗すぎるときは `点の間隔` で打ち直す（0 なら SOP の点のまま。400 点を超えると自動で間引く）。
- 生成したルートは `go2/route` の表にも書き出される（`表 → ルート保存` で JSON に保存できる）。

反映のされ方（約 15 Hz で SOP を見て、変わったら最大 10 Hz で go2pf へ送る）：

| 状況 | go2pf | map の表示 |
|---|---|---|
| 停止中（DISARMED / ESTOP / FAULT / FINISHED） | すぐ新しいルートに切り替わる | 灰色の線（使用中のルート） |
| 走行中（RUNNING / FINISHING / HOLD） | **今のルートのまま**。止めると切り替わる | 灰色＝使用中、**橙色＝保留中**の新しい形。`Status > ルート` に「保留中」 |
| フェンスの外に出る点・範囲外の speed・2 点未満 | **送らない**（前のルートのまま。他の設定は反映） | **赤色**の線。`Status > ルート` に理由 |

走行中にルートが変わると、ロボットが今いる場所と新しいルートの関係が急に変わって危ないので、切り替えは停止中だけにしている。
アニメーションする SOP でも、走行中は止めるまで反映されない点に注意。

### 3. 調整のしかた（カメラの高さ・範囲を最適にする）

`go2/overlay`（カメラ映像に重ね描き）と `go2/map`（俯瞰図）を見ながら行う。

1. **キャリブレーション**：基準マーカー 4 枚を映すと自動で計算・保存（`Status > キャリブレーション` が OK）。
   overlay に床の 1 m 格子・フェンス（赤）・ルート（白）が描かれるので、**実際の床の位置と合っているか**を見る。
   基準マーカーの位置（Area > Ref0〜3）を実測して入れ直すと、その場でやり直される。カメラを動かさなければ、保存後は基準マーカーを剥がしてよい。
2. **カメラの高さ・マーカーの高さ（視差補正）**：ロボットを区画の端の方に置き、overlay の**赤い点（推定した床の位置）が機体の中心の真下**に来るように
   `Camera > カメラの高さ`（レンズの床からの高さ）と `マーカーの高さ`（背中のマーカーの床からの高さ）を合わせる。
   中央では差が出にくく、端ほどよく分かる。天井 3 m・マーカー 0.4 m だと補正なしで中心から 2 m の位置で約 0.3 m ずれる。
   カメラを斜めに付けていて真下が画像の中心でないときは、`カメラ真下 = 画像中心` を Off にして `カメラ真下` に部屋座標を入れる（overlay の紫の十字）。
3. **向き**：ロボットの頭を +y（部屋の奥）に向けたとき map の向きの線が上を向くか。ずれていれば `向きの補正` で合わせる。
4. **安定度**：ロボットを止めた状態で `Status > 検出` の「揺れ」を見る。位置 1 cm 以下・向き 1〜2° 以下が目安。
   大きいときはピント・露出・照明・マーカーの大きさを見直す（展示の照明やプロジェクションの下で確認する）。
   検出率が 100% を切るときも同様。
5. **範囲**：`Area > ジオフェンス` を実際の区画に合わせる。判定はマーカー（機体中心）の位置なので、機体の半分（約 0.35 m）は内側に余裕を取る。
   map では赤い四角、overlay では床の上の赤い線で確認できる。ルートの点がフェンスの外に出る変更は受け付けられない。
6. **追従**：Mock または Dry で `Control` のゲインを動かし、map 上の目標点（紫の ×）と軌跡を見ながら調整する。

### 4. 運転

1. `Run > バックエンド` を選ぶ
   - **Mock**：ロボットなし。go2pf の中のシミュレーションが動く（map にはシミュレーションの位置を表示）
   - **Dry**：カメラの本物の位置で計算するが、ロボットには何も送らない（マーカーを持って歩いて確認する）
   - **Go2**：実機
2. `go2pf を起動` → `Status > go2pf プロセス` が「起動中」、`状態` が DISARMED、`設定の反映` が OK になるのを待つ
3. `アーム` で走行開始
4. 止めるときは **スペースキー**（TD のウィンドウにフォーカスがあるとき）か `非常停止` ボタン。再開は `アーム`
5. `go2pf を終了`（非常停止を送ってから終了する）。TD を閉じたときも同じことをする

**非常停止の経路について**：TD → go2pf は UDP なので、非常停止は 3 回送り、go2pf が ESTOP を返すまで 1.5 秒間送り続ける。
さらに TD が固まる・落ちると、位置が届かなくなり 0.5 秒で HOLD（停止）、操作元から 3 秒間なにも届かなければ go2pf は停止して終了する。
それでも**最後の手段はリモコン**。実機のときは必ず手に持っておく。

### 5. go2pf を別の PC で動かす場合

Go2 と有線でつながる PC が TD の PC と別なら、`Connect > go2pf の IP` にその PC の IP を入れ、
`起動コマンド上書き` に ssh などのコマンドを書く（`{backend}` `{port}` `{statusport}` `{interface}` `{config}` が置き換えられる）。例：
```
ssh user@192.168.123.50 "cd ~/Go2PathFollower && .venv/bin/python -m go2pf --backend {backend} --no-viz --port {port} --status-port {statusport} --interface {interface} --controller-timeout 3"
```
状態は go2pf から TD の PC の `状態の受信ポート`（9001）へ送られるので、両方の PC のファイアウォールで UDP 9000 / 9001 を開ける。
go2pf は最初に `/go2pf/config` を送ってきたホストだけを操作元として受け付ける（非常停止だけはどこからでも受け付ける）。

### 6. TD を使わない場合

`python touchdesigner/aruco_pose.py --camera 0 --host <go2pf の IP> --camera-height 3.0 --marker-height 0.4 --show` で、
カメラから直接 `/dog/pose` を送れる。go2pf はターミナルから `--route` を付けて起動し、キー操作（a / space / q）で動かす。

### TD 側の未確認点

go2ctl.py の処理（検出・送信・起動/終了・状態表示・描画）は、TD のコールバックと同じ順に呼ぶ偽物の TD で、
本物の go2pf を起動して確認してある（`tests/test_go2ctl.py`。TD 同梱の Python 3.11 / numpy 2.1 / OpenCV 4.11 でも動作確認）。
ただし **build_td.py が TD の中で正しくネットワークを作れるかは、TD 上ではまだ確かめていない**。
TD のバージョンによってパラメータ名が違うと `[注意]` が表示されるので、その場合は次を手で設定する：

| オペレータ | 設定 |
|---|---|
| `go2ctl`（Text DAT） | File = touchdesigner/go2ctl.py、Sync to File = On |
| `frame`（Execute DAT） | Start / Frame Start / Exit = On |
| `pars`（Parameter Execute DAT） | OPs = go2、Parameters = `*`、Custom = On、Value Change = On、On Pulse = On |
| `keys`（Keyboard In DAT） | Callbacks DAT = keys_cb |
| `map` / `overlay`（Script TOP） | Callbacks DAT = map_cb / overlay_cb |
| `route_shape`（Circle SOP） | Type = Polygon、Radius = 1.5、Divisions = 48（例なので形は何でもよい） |
| `go2` の `Route > ルートの SOP` | `route_sop` |

SOP の点の読み方（`point.x / y / z`、`prims[0]` の頂点順、`prim.closed`）も TD 上では未確認。
閉じた形なのにループにならないときは `Run > ループ走行` を On にすれば同じ動きになる。

重いときは `mono` と `cam` の間に Resolution TOP を入れて 1280×720 程度にする（入れたらキャリブレーションし直し）。

## 実機で動かす（Go2 EDU の拡張 PC で go2pf、Mac の TD から操作）

```
Mac（TouchDesigner, カメラ）──Wi-Fi──▶ Go2 の拡張 PC（Jetson, go2pf）──eth0──▶ Go2 本体
     /dog/pose・設定・アーム・非常停止（UDP 9000）→   ← 状態（UDP 9001）
```

**必ずリモコンを手に持ち、周囲を空けてから**。最初は遅い速度で。

### A. 準備（初回のみ）

1. **Jetson の IP とユーザー名**を確認する（Mac と同じ Wi-Fi につながっていること）。以下 `unitree@192.168.11.50` は例。
2. **ssh を鍵認証にする**（TD からはパスワードを入力できないため）。Mac のターミナルで：
   ```bash
   ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
   ssh-copy-id unitree@192.168.11.50
   ssh unitree@192.168.11.50 true
   ```
   最後の行がパスワードを聞かずに終われば OK。
3. **ソースを送る**（Mac で）：
   ```bash
   cd /Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower && tools/deploy_go2.sh unitree@192.168.11.50
   ```
   コードを直したら、この行をもう一度実行すれば更新される。
4. **Jetson のセットアップ**（Mac から実行。cyclonedds のビルドで数分かかる）：
   ```bash
   ssh -t unitree@192.168.11.50 'cd Go2PathFollower && bash tools/setup_go2_pc.sh'
   ```
   最後に `SDK OK`、テストの `OK`、そして **eth0 が 192.168.123.x** と出れば準備完了。
   `python3-venv` や `cmake` がないと言われたら、表示どおり `sudo apt install ...` してからもう一度。
5. **向きの確認**（1 回だけ。ロボットを立たせ、通常の歩行モードで、リモコンを持って）：
   ```bash
   ssh -t unitree@192.168.11.50 'cd Go2PathFollower && .venv/bin/python tools/go2_check.py eth0'
   ```
   - `vyaw=+0.3` で **上から見て反時計回り**、`vx` で前進、`vy` で左に動くか
   - 指令途絶テスト（任意）で、送信が止まったときにロボットが自分で止まるか
   結果が違ったら知らせてほしい（符号を直す）。

### B. TD の設定

`build_td.py` をもう一度実行して新しいパラメータ（Connect > ssh 先 など）を足してから：

| パラメータ | 値 |
|---|---|
| Connect > ssh 先 | `unitree@192.168.11.50` |
| Connect > ssh 先の Go2PathFollower フォルダ | `Go2PathFollower` |
| Connect > Go2 の NIC 名 | `eth0` |
| Control > 前進速度の上限 / 既定の前進速度 | 最初は **0.3 / 0.2** 程度 |

ssh 先を入れると、OSC の送り先も自動でそのホストになる（Connect > go2pf の IP は使わない）。
状態は Jetson から Mac の UDP 9001 に返ってくるので、macOS に「TouchDesigner への受信を許可しますか」と聞かれたら許可する
（システム設定 > ネットワーク > ファイアウォール）。

### C. 走らせる

1. **Dry で通しで確認**：`Run > バックエンド = Dry` → `go2pf を起動`。
   `Status > go2pf プロセス` が「起動中（dry, ssh …）」、`設定の反映` が OK、`状態` が DISARMED になること。
   アームして、ロボットを手で持つ（またはマーカーを持って歩く）と指令が出ること、マーカーを隠すと HOLD になることを確認。
   Wi-Fi が不安定だと HOLD が頻繁に出る（`Status > 理由` に「途絶」）。そのときは Wi-Fi を見直すか、`Safety > 見失いタイムアウト` を少し延ばす。
   確認したら `go2pf を終了`。
2. **Go2**：ロボットを立たせて通常の歩行モードにし、ルートの始点近くに置く。`バックエンド = Go2` → `go2pf を起動`。
   `logs/go2pf_td.log`（Mac 側）の「モーションモード」の行が空でないことを確認。
3. `アーム`。**スペースキー（TD のウィンドウを選んでおく）か リモコンでいつでも止められる状態で**見守る。
4. 終わったら `go2pf を終了`。

うまくいかないとき：

| 症状 | 見るところ |
|---|---|
| go2pf プロセスが「ssh に失敗」 | 鍵認証（A-2）、IP、ユーザー名。`logs/go2pf_td.log` に ssh のエラーが出る |
| 「起動中」だが 設定の反映 が「応答がありません」 | Mac のファイアウォール（UDP 9001 の受信）、Jetson と Mac が同じネットワークか |
| アームしても動かない | 歩行モード（アプリ / リモコン）、`go2pf_td.log` の Move / StopMove のエラー |
| 逆に回る | A-5 の結果を知らせる（vyaw の符号） |

## 実機での起動手順（ターミナルから動かす場合）

TD から動かす場合は上の「運転」の手順でよい。ここではターミナルから go2pf を直接起動する場合を書く。
どちらの場合も 1 と 2 の確認は最初に一度行う。


1. **TouchDesigner の確認**：ロボットを区画の中で手で動かしながら
   ```bash
   .venv/bin/python tools/osc_monitor.py --port 9000
   ```
   - 約 30 Hz で届いているか
   - 右へ動かすと x が増え、前（部屋の +y 方向）へ動かすと y が増えるか
   - ロボットの頭を +y に向けたとき yaw ≈ 0、左（-x）に向けたとき yaw ≈ +1.57 になるか
   - マーカーを隠すと found=0 になるか
2. **（初回のみ）符号の確認**：周囲を空けてリモコンを持ち、`python tools/go2_check.py eth0`。
   `vyaw > 0` で上から見て反時計回り、`vx > 0` で前進、`vy > 0` で左へ動くことを確かめる（下の「SDK について」参照）。
3. Go2 を立たせ、アプリかリモコンで通常の歩行モードにしておく（このプログラムはモードを切り替えない）。
4. ロボットをルートの始点付近に置き、起動：
   ```bash
   .venv/bin/python -m go2pf --backend go2 --route routes/example_route.json
   ```
   起動直後は `DISARMED`。ロボットには何も送らない。
5. 画面の位置と向きが正しいことを確認してから `a` でアーム。
6. 止めるときは `space`（非常停止）。再開は `a`。終了は `q` か Ctrl-C（どちらも停止指令を送ってから終わる）。

キーはターミナルか可視化ウィンドウのうち、**フォーカスがある方**にしか届かない。どちらかに必ずフォーカスを置いておくこと。

## 状態と安全動作

| 状態 | 意味 | 指令 | 抜け方 |
|---|---|---|---|
| DISARMED | 起動直後 | 送らない | `a` |
| RUNNING | 追従中 | 毎周期 `Move` | — |
| HOLD | found=0、または最後の受信から `pose_timeout` 超 | 入るときに `StopMove`、以後送らない | 位置が戻れば自動で RUNNING（`resume_after_tracking_loss: false` なら `a` が必要） |
| FINISHING | 最終点に到達 | 加速度制限に沿って 0 まで減速 | 自動で FINISHED |
| FINISHED | 停止済み | `StopMove` 後は送らない | `a` でルートの最初から |
| ESTOP | スペースキー | 押した瞬間に `StopMove`、以後送らない | `a`（続きから） |
| FAULT | ジオフェンスの外、外へ向かう指令、制御ループの例外 | `StopMove`、以後送らない | `a`（フェンス内にいるときのみ） |

- アームは「位置を受信していて found=1、かつフェンス内」のときだけ受け付ける。
- 「外へ向かう指令」の判定：今回の指令を部屋座標に直し、`geofence_horizon` 秒進んだ位置がフェンス外なら止める。
- 指令値は加速度制限のあと、もう一度 `speed_max`・`vy_max`・`vyaw_max` でクリップしてから送る。
  後退（vx < 0）は送らない。設定ファイルでも `speed_max ≤ 1.0`、`vy_max ≤ 0.5`、`vyaw_max ≤ 2.0` を超えられない。
- 終了時（`q`、Ctrl-C、SIGTERM、例外）は状態に関係なく必ず `StopMove` を送る。
  起動後一度もアームしていなくても終了時の停止指令だけは送る（停止指令なので害はないと判断）。
- 非常停止は制御ループを待たず、キーを押したスレッドから直接 `StopMove` を送る。

## パラメータ（config/default.yaml）

| キー | 既定値 | 意味 |
|---|---|---|
| `osc.port` / `osc.address` | 9000 / `/dog/pose` | 受信ポートとアドレス |
| `control.rate_hz` | 30 | 制御周期 [Hz]。毎周期、受信済みの最新位置を使う |
| `control.lookahead` | 0.5 | 先読み距離 [m]。大きいほど滑らかだが角を内側にショートカットする |
| `control.speed` | 0.3 | 既定の前進速度 [m/s]（ルートの点に `speed` がない区間） |
| `control.speed_max` | 0.6 | 前進速度の上限 [m/s]。ルートの `speed` もこれを超えられない |
| `control.k_yaw` | 1.5 | 旋回ゲイン：`vyaw = clamp(k_yaw * e, ±vyaw_max)` |
| `control.vyaw_max` | 1.0 | 旋回速度の上限 [rad/s] |
| `control.k_lateral` | 0.0 | 横ずれ補正ゲイン [1/s]。0 なら vy は常に 0 |
| `control.vy_max` | 0.2 | 横移動の上限 [m/s] |
| `control.accel_vx` / `accel_vy` / `accel_vyaw` | 0.5 / 0.5 / 2.0 | 1 周期あたりの変化量 = 加速度 × dt |
| `control.goal_radius` | 0.25 | 最終点の到達判定半径 [m] |
| `control.loop` | false | ループ走行（`--loop` でも指定可） |
| `control.search_back` / `search_ahead` | 0.2 / 1.0 | 最近点を探す範囲（現在の進捗からの弧長 [m]） |
| `safety.pose_timeout` | 0.5 | 位置が途絶えたとみなす時間 [s] |
| `safety.resume_after_tracking_loss` | true | 見失いからの自動再開 |
| `safety.geofence.*` | ±2.3 | 区画の範囲 [m]（マーカー位置で判定） |
| `safety.geofence_horizon` | 0.5 | 外へ向かう指令の予測時間 [s] |
| `go2.interface` | eth0 | ロボットにつながる NIC |
| `go2.domain_id` | 0 | DDS ドメイン（公式サンプルと同じ） |
| `go2.rpc_timeout` | 1.0 | `StopMove` などの応答待ち [s] |
| `mock.*` | — | シミュレーションのノイズ・遅延・見失い・初期位置 |
| `remote.status_port` | 9001 | 状態を送り返すポート（送り先は操作元の IP） |
| `remote.allow_hosts` | [] | 操作を受け付けるホスト。空なら最初に `/go2pf/config` を送ってきたホストだけ |
| `remote.controller_timeout` | 0 | 操作元からこの秒数なにも届かなければ停止して終了（TD から起動すると 3） |
| `log.dir` | logs | CSV ログの保存先 |

### ルート JSON

```json
{"points": [
  {"x": -1.5, "y": -1.5},
  {"x": -1.5, "y":  1.5, "speed": 0.3},
  {"x": -0.6, "y":  1.5, "speed": 0.2}
]}
```

点の配列だけ（`[{...}, ...]`）でもよい。`speed` は「その点から次の点までの区間」の前進速度。
ジオフェンスの外の点があると起動時にエラーになる。

### 経路追従の計算

1. 現在の進捗 s の付近（`[s - search_back, s + search_ahead]`）で、折れ線上の最近点を探す。
   進捗は戻さない（角やノイズで最近点が前後に揺れて目標点がぶれるのを防ぐため）。
   範囲を絞っているので、8 の字のようにルートが交差しても別の枝へ飛ばない。
2. 最近点から弧長で `lookahead` 先の点を目標 (tx, ty) にする（終点を越えるときは終点、ループ時は先頭へ回る）。
3. `θt = atan2(-(tx - x), ty - y)`、`e = wrap(θt - yaw)`
4. `vyaw = clamp(k_yaw * e, ±vyaw_max)`、`vx = speed * max(0, cos e)`
5. `k_lateral > 0` のときは横ずれを戻す向きの速度をロボットの左右成分に変換して `vy` にする。
6. 加速度制限 → 上限クリップ → 安全判定 → 送信。
7. 終点から `goal_radius` 以内（かつ進捗が終点付近）で FINISHING。

### ログ（logs/run_YYYYmmdd_HHMMSS.csv）

制御周期ごとに 1 行：`wall_time, t, state, reason, x, y, yaw, found, pose_age, s, tx, ty, heading_error, cross_track,
raw_vx, raw_vy, raw_vyaw`（追従計算の出力）`, vx, vy, vyaw`（実際に送った値）`, sent`（送ったか）。

## SDK について（確認した内容と、仕様との食い違い・未確認点）

`unitree_sdk2_python` の公式リポジトリ（2026-09-21 のコミット `814556d`）のソースで確認した：

- 初期化：`ChannelFactoryInitialize(0, "eth0")` → `SportClient()` → `SetTimeout(秒)` → `Init()`（`example/go2/high_level/go2_sport_client.py` と同じ）
- `SportClient.Move(vx, vy, vyaw)` は JSON `{"x": vx, "y": vy, "z": vyaw}` を **応答を待たずに** 送る（`_CallNoReply`）。戻り値 0 は「送信できた」の意味で、ロボットが受理したかは分からない。
- `SportClient.StopMove()` は **応答を待つ** RPC で、`SetTimeout` の時間まで待つことがある。戻り値 0 が成功。
- `MotionSwitcherClient.CheckMode()` で現在のモーションモードを取得できる。起動時に表示だけする。

**未確認・注意点**（実機で確認してから展示に使うこと）：

1. **vyaw の符号**：SDK のソースには回転方向の定義が書かれていない。ROS の慣例どおり反時計回りが正と想定して実装したが、`tools/go2_check.py` で確認すること。逆なら `Go2Backend.move` で符号を反転する。
2. **指令が途絶えたときの挙動**：`Move` を送り続けなかったとき、ロボットが自分で止まるのか歩き続けるのかは SDK からは分からない。
   このプログラムは止めるときに必ず `StopMove` を送り、例外時も終了時も送るが、プロセスが強制終了（`kill -9`、PC の電源断）された場合は送れない。
   `tools/go2_check.py` の手順 4 で確認し、歩き続けるようならリモコンでの停止を運用に組み込むこと。
3. **モーションモード**：ファームウェアによっては sport サービスが動いている（通常の歩行）モードでないと `Move` が効かない。
   このプログラムはモードを自動で切り替えない。起動ログの「モーションモード」が空なら、アプリ／リモコンで通常モードにする。
4. **段差を越える**：このコントローラは歩容を変えない（Go2 の既定の歩行のまま速度指令を送る）。どの高さの段差を越えられるかは Go2 側の性能で、ここでは検証していない。
   SDK には `ClassicWalk`、`StaticWalk`、`FreeWalk` などの歩容 API もあるが、段差への効果が確認できないので使っていない。段差の区間はルートの `speed` を下げておく。
   段差を越えるときに機体が傾くと、天井カメラで見たマーカー位置がずれることがある。
5. **ジオフェンスと機体の大きさ**：判定はマーカー（機体中心と仮定）の位置で行う。Go2 は全長約 0.7 m なので、±2.3 m では鼻先が ±2.65 m 付近まで出る。
   区画が 5 m 四方（±2.5 m）で壁がある場合は、フェンスを ±2.0 m 程度に狭めるか、ルートを ±1.8 m 以内に収めることを勧める（例のルートは ±1.5 m）。
   マーカーが機体中心にない場合は、TouchDesigner 側で中心に補正してから送ってほしい。
6. 実行するのは、`eth0` でロボットにつながっていて、かつ TouchDesigner からの OSC が届く PC。Go2 EDU の内蔵 PC で動かす場合は、NIC 名と OSC の宛先 IP を合わせる。

この実装は macOS + Python 3.12 で Mock と単体テストのみ確認済み。**実機（Go2Backend）ではまだ動かしていない。**

## 将来の拡張：人間を待つ機能（設計のみ）

`/human/pose`（2 つ目のマーカー）を受け取り、人間がルート上で一定距離以上遅れたらロボットが待つ。

- **受信**：`OscPoseReceiver` は `{アドレス: PoseBuffer}` を受け取る形にしてあるので、`/human/pose` 用の `PoseBuffer` を足すだけでよい。
- **人間の進捗**：同じ `Polyline.project` で人間の位置をルートに投影し、弧長 `s_human` を得る（人間用に別の `PurePursuit` 相当の進捗トラッカーを持たせると、交差点での飛びも防げる）。
- **判定**：`gap = s_robot - s_human`（ループ時は周回数込み）。`gap > wait_distance` で待ち、`gap < resume_distance`（ヒステリシス）で再開。
- **組み込み場所**：`Supervisor._drive` で、追従計算のあとの目標速度に `0〜1` の倍率をかける（`WAIT` 状態を足すか、RUNNING のまま目標速度を 0 にして加速度制限で止める）。
  急停止ではなく減速で止まるので、見ている人に自然に見える。
- **人間を見失ったとき**：ロボットは待つ側に倒す（`s_human` 不明なら待つ）。安全監視（ロボット自身の見失い・フェンス）はこれまでどおり最優先。
