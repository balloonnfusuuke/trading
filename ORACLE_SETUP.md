# Oracle Cloud 無料VM セットアップガイド
## 米国株リアルタイム監視（22:30〜5:00 JST）

---

## ① Oracle Cloud アカウント作成

1. https://www.oracle.com/jp/cloud/free/ にアクセス
2. 「無料で始める」でアカウント作成（クレジットカード必要だが請求なし）
3. ホームリージョンは **Japan East (Tokyo)** を選択

---

## ② 無料VMを作成

1. 「コンピュート」→「インスタンス」→「インスタンスの作成」
2. 設定：
   - イメージ: **Ubuntu 22.04**（推奨）
   - シェイプ: **VM.Standard.A1.Flex**（Always Free）
   - CPU: 1 OCPU / メモリ: 6GB
3. SSHキーを保存（後でログインに使用）
4. 作成後、パブリックIPアドレスをメモする

---

## ③ VMに接続してPythonをセットアップ

```bash
# SSH接続（WindowsのPowerShellから）
ssh -i C:\Users\Owner\.ssh\id_rsa ubuntu@<VM_IPアドレス>

# Python・pipのインストール
sudo apt update && sudo apt install -y python3 python3-pip git

# 作業ディレクトリ作成
mkdir ~/trading && cd ~/trading
```

---

## ④ Trading フォルダのファイルをVMにコピー

PCのPowerShellから以下を実行：

```powershell
# 必要ファイルをVMにコピー（SCPで転送）
$ip = "<VM_IPアドレス>"
$key = "C:\Users\Owner\.ssh\id_rsa"
$src = "C:\Users\Owner\OneDrive\ドキュメント\Trading"

scp -i $key "$src\monitor.py"    ubuntu@${ip}:~/trading/
scp -i $key "$src\config.py"     ubuntu@${ip}:~/trading/
scp -i $key "$src\positions.json" ubuntu@${ip}:~/trading/

# Pythonライブラリインストール
ssh -i $key ubuntu@$ip "pip3 install requests pandas"
```

---

## ⑤ positions.json の自動同期（OneDrive → Oracle VM）

PCの `positions.json` が更新されるたびに Oracle VM に転送する必要があります。

### 方法A: main.py から自動転送（推奨）

`config.py` に以下を追加：

```python
# Oracle Cloud VM の接続情報（オプション）
ORACLE_VM_IP  = "<VM_IPアドレス>"          # 例: "123.45.67.89"
ORACLE_VM_KEY = r"C:\Users\Owner\.ssh\id_rsa"  # SSHキーのパス
ORACLE_VM_USER = "ubuntu"
```

`main.py` の `run()` 関数の最後（`add_new_positions` の後）に以下を追加すると、
スクリーニング後に自動でVMに転送されます：

```python
# positions.json を Oracle Cloud VM に転送
sync_positions_to_vm()
```

> この関数の実装は別途追加してください（Cursor Agent に依頼）

### 方法B: 手動で転送（一時的な運用）

PCで毎朝・毎夕スクリーニング後に手動でコピー：

```powershell
scp -i C:\Users\Owner\.ssh\id_rsa `
    "C:\Users\Owner\OneDrive\ドキュメント\Trading\positions.json" `
    ubuntu@<VM_IP>:~/trading/positions.json
```

---

## ⑥ cron で5分おき監視を設定

VMにSSHして：

```bash
crontab -e
```

以下を追記（UTC 基準）：

```cron
# 日本株 9:00-15:30 JST = 0:00-6:30 UTC（月〜金）
*/5 0-6 * * 1-5 cd /home/ubuntu/trading && python3 monitor.py >> monitor_cron.log 2>&1

# 米国株 22:30-翌5:00 JST = 13:30-翌20:00 UTC (EDT)（月〜金）
*/5 13-23 * * 1-5 cd /home/ubuntu/trading && python3 monitor.py >> monitor_cron.log 2>&1
*/5 0-20  * * 2-6 cd /home/ubuntu/trading && python3 monitor.py >> monitor_cron.log 2>&1
```

---

## ⑦ 動作テスト

VMのSSHから：

```bash
cd ~/trading
python3 monitor.py --test
# → Discord に「接続テスト」通知が届けばOK

python3 monitor.py --force
# → 取引時間外でも強制チェック実行
```

---

## ⑧ ファイアウォール設定（不要な場合が多い）

Oracle Cloud は デフォルトでポート22（SSH）が開いています。
監視スクリプトは外から受信しないので追加設定は不要です。

---

## まとめ：最終的な役割分担

| 場所 | 実行内容 | 時刻 |
|------|---------|------|
| PC（Task Scheduler） | 米国株スクリーニング | 4:40 AM JST |
| PC（Task Scheduler） | 日本株スクリーニング | 15:10 JST |
| PC（Task Scheduler） | 日本株ポジション監視 | 9:00-15:30、5分おき |
| **Oracle Cloud VM（cron）** | **米国株ポジション監視** | **22:30-5:00、5分おき** |
| **Oracle Cloud VM（cron）** | **日本株ポジション監視** | **9:00-15:30、5分おき** |

Oracle Cloud VM が安定したら PC の日本株監視タスクも無効にできます。
