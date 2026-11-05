# ZOUNOHANA2026 — Go2 サウンドアート / シミュレーション環境

## 背景
- Unitree Go2 を使ったサウンドアート。Go2 は別団体からの借り物で、実機に触れるのは**毎週土曜だけ**。
- 平日の開発用に、この Windows PC (RTX 4090 24GB) の Isaac Sim で Go2 を仮想的に動かす。
- **機械学習 (学習) はしない**。学習済み歩行ポリシーを動かすだけのシミュレータとして使う。
- 実機は**ラジコン操作** (Unitree 内蔵の歩行制御 = sport mode) で動かしている。シミュレータでもラジコン的な操作 (前後・左右・旋回の速度指令) ができればよい。
- 音側は Mac の Max/MSP (`innu.maxpat`) と Ableton。この PC から Mac へ LAN 経由で OSC を送る。

## 実機の現状のデータの流れ
Go2 本体 → Wi-Fi → Mac の Max (`innu.maxpat`)

### innu.maxpat が受け取る形式 (シミュレータもこれに合わせる。パッチは変更しない)
- `[udpreceive 9871]` → `[route /gait/force]`
- `/gait/force i i i i` — 実機 `LowState.foot_force` の生の値 (整数)、並び順 **FR FL RR RL**
- 値の目安は 0〜70 程度 (パッチ内で `scale 0 70 0 127` してベロシティにしている)
- パッチのメッセージボックスに残っていた値: `23 17 36 26` (静止立ち付近と推測。土曜に実測して確定させる)

### パッチ内の処理 (`p thr`)
- 接地判定のしきい値: FR > 22, **FL × 2 > 27** (実機の FL センサは値が約半分), RR > 24, RL > 25
- しきい値を超えた状態が `metro 6` × `counter > 3` (約 18〜24ms) 続いたら接地とみなす。値が下がるとカウンタをリセット
- 接地 → `/note/FR` 等 (36) と `/vel/FR` 等 (力を 0-127 にスケール) を `[udpsend 127.0.0.1 6060]` で Ableton へ
- 400ms 後に `/vel/*` を 0 にして、次の接地を受け付ける (`onebang` + `del 400`)

## シミュレータに求めること
1. Go2 をキーボード (できればゲームパッド) でラジコン操作できる
2. 4 本の足先の接触力を取り、**上の `/gait/force` と同じ形式・順番・値の範囲**で Mac の Max に OSC 送信する
3. 障害物や会場モデル (USD) を置ける
4. 力の単位が違う (シミュレータは N、実機は生の値) ので、実機の静止立ちの値に合わせて足ごとの倍率で変換する

## この PC の環境
- Isaac Sim **6.2.0** (pip 版)、仮想環境は `env_isaacsim\` (Python 3.12)、PyTorch 2.11 (CUDA 12.8)
- 起動: `.\env_isaacsim\Scripts\activate` してから `isaacsim` (GUI) または `python script.py` (スタンドアロン)
- EULA: 環境変数 `OMNI_KIT_ACCEPT_EULA=YES`

## Isaac Sim 6.2 の API メモ (ソースで確認済み)
- Go2 の歩行: `from isaacsim.robot.policy.examples import RobotPolicyRunner, get_go2_spec, PolicyEnvConfig`
  - `RobotPolicyRunner(get_go2_spec(), prim_path="/World/Go2", position=[0,0,0.5])` → `.spawn()`
  - 物理コールバック (`SimulationManager.register_callback(fn, IsaacEvents.POST_PHYSICS_STEP)`) 内で、初回は `restart_from_default_state(cmd)`、以降は `step(dt, cmd)`。`cmd = [vx, vy, yaw_rate]`
  - 物理 200Hz / 描画 50Hz。ロボットの USD は `{assets}/Isaac/IsaacLab/Robots/Unitree/Go2/go2.usd`
  - 旧 API の `Go2FlatTerrainPolicy` は 6.1 以降廃止 (呼ぶとエラー)
- 参考になる公式スクリプト (`pip show isaacsim` の場所の下の `standalone_examples`):
  - `api/isaacsim.robot.policy.examples/go2_standalone.py` — Go2 をスクリプトで歩かせる
  - `api/isaacsim.sensors.experimental.physics/contact_sensor.py` — 接触センサ
  - GUI の例 (キーボード操作): Robotics Examples > POLICY > Go2
- 接触センサ: `from isaacsim.sensors.experimental.physics import Contact, ContactSensor`
  - `ContactSensor(Contact.create("<剛体の下>/contact_sensor", min_threshold=0, max_threshold=1e7, radius=-1))`
  - `.get_data()["force"]` で力の大きさ [N]、`["in_contact"]` で接触の有無
  - 拡張 `isaacsim.sensors.experimental.physics` を有効にする (`--enable` 引数)
- Go2 の足先は `FR_foot` / `FL_foot` / `RR_foot` / `RL_foot` という名前の剛体のはず (未確認。無ければ `*_calf`)

## 注意
- シミュレータの歩き方は実機 (Unitree 内蔵の歩行制御) と違う。一歩の周期や足の着く順番が違うので、リズムの最終調整は土曜の実機で行う
- 平地用のポリシーなので、大きな段差や急な坂では転びやすい
