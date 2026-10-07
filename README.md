# Kasumin

Windows の Web カメラを、ブラウザから見られるネットワークカメラにする小さなアプリです。
IP カメラと同じ MJPEG 方式で配信するので、ブラウザのほか VLC・OBS・Home Assistant などからもそのまま開けます。

## 特長

- ブラウザだけで視聴できる(クライアント側のインストール不要、スマホからも OK)
- 複数クライアントの同時視聴。カメラは 1 回しか開かず、JPEG エンコードもフレームごとに 1 回で共有
- スナップショット URL とステータス JSON を提供
- カメラが抜かれた・他アプリに取られた場合も自動で再接続(その間は NO SIGNAL 画像を配信)
- Basic 認証、回転・反転、日時の焼き込み、視聴者がいない間のカメラ解放
- 依存ライブラリは OpenCV だけ

## 必要なもの

- Windows 10 / 11
- Python 3.10 以上(Microsoft Store 版でも可)
- Web カメラ

## セットアップと起動

`start.bat` をダブルクリックするか、コマンドプロンプトで実行します。
初回は仮想環境の作成と依存パッケージのインストールを自動で行います。

```bat
start.bat
```

手動でセットアップする場合:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m kasumin
```

起動すると次のように表示されるので、同じ LAN 内の PC やスマホのブラウザで URL を開きます。

```
Kasumin v0.1.0 - ネットワークカメラを起動しました

  ブラウザで開く:
    http://127.0.0.1:8080/
    http://192.168.1.23:8080/

  MJPEG ストリーム : http://192.168.1.23:8080/stream
  スナップショット : http://192.168.1.23:8080/snapshot.jpg
  ステータス (JSON): http://192.168.1.23:8080/api/status
```

初回起動時に Windows ファイアウォールの許可ダイアログが出た場合は「アクセスを許可する」を選んでください。

## 主なオプション

`start.bat` の後ろに付けるか、`python -m kasumin` に渡します。`--help` で全一覧が見られます。

| オプション | 既定値 | 説明 |
| --- | --- | --- |
| `--port`, `-p` | `8080` | 待ち受けポート |
| `--host` | `0.0.0.0` | 待ち受けアドレス。自分の PC からだけ見るなら `127.0.0.1` |
| `--camera`, `-c` | `0` | カメラのインデックス。`--list-cameras` で確認 |
| `--width` / `--height` | `1280` / `720` | 要求する解像度。`0` でカメラの既定値 |
| `--fps` | `30` | 要求するフレームレート |
| `--quality`, `-q` | `80` | JPEG 品質 (1〜100)。下げると帯域と CPU を節約できる |
| `--password` | なし | 指定すると Basic 認証が有効になる(ユーザー名は `--user`、既定 `admin`) |
| `--rotate` | `0` | 回転角度 (0 / 90 / 180 / 270) |
| `--flip` | `none` | 反転 (`h` 左右 / `v` 上下 / `hv` 両方) |
| `--timestamp` | off | 映像の左下に日時を焼き込む |
| `--idle-timeout SEC` | `0` | 視聴者がいない状態が SEC 秒続いたらカメラを解放する(LED が消える) |
| `--backend` | `dshow` | OpenCV のキャプチャバックエンド (`dshow` / `msmf` / `any`) |
| `--fourcc` | なし | カメラに要求するピクセル形式。高解像度で fps が出ないときは `MJPG` を試す |
| `--list-cameras` | | 利用できるカメラを表示して終了 |

例: 2 台目のカメラを 720p / 15fps、パスワード付きで 9000 番ポートに公開する

```bat
start.bat --camera 1 --fps 15 --port 9000 --password himitsu --timestamp
```

パスワードは環境変数 `KASUMIN_PASSWORD`(ユーザー名は `KASUMIN_USER`)でも渡せます。

## エンドポイント

| パス | 内容 |
| --- | --- |
| `/` | ブラウザ用の視聴ページ |
| `/stream` | MJPEG ストリーム (`multipart/x-mixed-replace`)。`?fps=5` のように配信レートを制限できる |
| `/snapshot.jpg` | 最新フレームの JPEG 1 枚 |
| `/api/status` | 解像度・実測 fps・視聴者数・稼働時間などの JSON |

`/stream.mjpg` `/video` `/snapshot` `/image.jpg` などの別名も用意しています(IP カメラ向けアプリの既定パスに合わせやすいように)。

## 他のアプリから使う

- **VLC**: メディア → ネットワークストリームを開く → `http://<IP>:8080/stream`
- **OBS**: メディアソースを追加 → 「ローカルファイル」のチェックを外し、入力に `http://<IP>:8080/stream`
- **ffmpeg**: `ffmpeg -i http://<IP>:8080/stream -c:v copy out.mkv`(再エンコードなしで録画)
- **Home Assistant**: MJPEG IP Camera 統合で `mjpeg_url` に `/stream`、`still_image_url` に `/snapshot.jpg`
- **Python / OpenCV**: `cv2.VideoCapture("http://<IP>:8080/stream")`
- Basic 認証を有効にした場合は `http://admin:password@<IP>:8080/stream` の形式で指定します

## ログオン時に自動起動する

タスク スケジューラに登録する例(管理者権限は不要):

```bat
schtasks /Create /TN Kasumin /SC ONLOGON /TR "\"C:\path\to\Kasumin\start.bat\" --password himitsu"
```

コンソール画面を出したくない場合は `start.bat` の代わりに
`"C:\path\to\Kasumin\.venv\Scripts\pythonw.exe" -m kasumin` を登録してください(ログは表示されません)。

## セキュリティについて

- 通信は暗号化されません(HTTP)。信頼できる LAN 内での利用を想定しています
- ルーターのポート開放でインターネットに直接公開しないでください。外から見たい場合は VPN(Tailscale など)や、HTTPS を終端するリバースプロキシを使ってください
- LAN 内でも、他の人が見られる状況なら `--password` を付けることを推奨します
- 自分の PC からだけ使うなら `--host 127.0.0.1` で外部からの接続を遮断できます

## トラブルシューティング

**カメラが開けない / NO SIGNAL のまま**

- Teams や Zoom など他のアプリがカメラを使っていないか確認してください(Windows ではカメラを同時に使えるアプリは 1 つだけです)
- 「設定 → プライバシーとセキュリティ → カメラ」で、カメラへのアクセスと「デスクトップ アプリがカメラにアクセスできるようにする」が ON になっているか確認してください
- `start.bat --list-cameras` で認識されているカメラとインデックスを確認し、`--camera N` を指定してください
- `--backend msmf` を試してください

**fps が低い**

- `--fourcc MJPG` を付けると、多くの USB カメラで高解像度でも 30fps 出るようになります
- `--width 640 --height 480` のように解像度を下げる、`--quality 60` のように品質を下げる

**他の PC から見えない**

- Windows ファイアウォールで Python の受信が許可されているか確認してください。手動で許可する場合:
  `netsh advfirewall firewall add rule name="Kasumin" dir=in action=allow protocol=TCP localport=8080`
- ネットワークの種類が「パブリック」になっていると遮断されることがあります。「プライベート」に変更してください

**ポートが使用中と言われる**

- `--port 8081` のように別のポートを指定してください

## 開発

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

テストは合成フレームを使うので、カメラがない環境でも実行できます。

構成:

```
kasumin/
  camera.py     カメラのキャプチャスレッド、JPEG エンコードの共有、再接続
  server.py     HTTP サーバー (MJPEG ストリーム、スナップショット、ステータス、Basic 認証)
  devices.py    カメラデバイスの探索
  cli.py        コマンドライン引数と起動処理
  static/index.html  ブラウザ用の視聴ページ
tests/          サーバーとカメラ管理の結合テスト
```
