# Kasumin

Windows の Web カメラを、ブラウザから見られるネットワークカメラにする小さなアプリです。
IP カメラと同じ MJPEG 方式で配信するので、ブラウザのほか VLC・OBS・Home Assistant などからもそのまま開けます。

## 特長

- ブラウザだけで視聴できる(クライアント側のインストール不要、スマホからも OK)
- 複数クライアントの同時視聴。カメラは 1 回しか開かず、JPEG エンコードもフレームごとに 1 回で共有
- 複数の Web カメラを同時に配信できる(ブラウザでは一覧表示と 1 台ずつの拡大表示、アプリ用にはカメラごとの URL)
- スナップショット URL とステータス JSON を提供
- カメラが抜かれた・他アプリに取られた場合も自動で再接続(その間は NO SIGNAL 画像を配信)
- LAN の外から安全に見られる: パスキー / セキュリティキーによる FIDO2 認証、HTTPS、アプリ用のアクセストークン
- Basic 認証(LAN 内向け)、回転・反転、日時の焼き込み、視聴者がいない間のカメラ解放
- 依存ライブラリは OpenCV と fido2 (FIDO2 認証用) だけ

## 必要なもの

- Windows 10 / 11
- Python 3.10 以上(Microsoft Store 版でも可)
- Web カメラ

## セットアップと起動

`start.bat` をダブルクリックするか、コマンドプロンプトで実行します。
初回(と `requirements.txt` が更新されたとき)は、仮想環境の作成と依存パッケージのインストールを自動で行います。

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
Kasumin v0.4.0 - ネットワークカメラを起動しました

  ブラウザで開く:
    http://127.0.0.1:8080/
    http://192.168.1.23:8080/

  カメラ           : [0] Integrated Webcam
  MJPEG ストリーム : http://192.168.1.23:8080/stream
  スナップショット : http://192.168.1.23:8080/snapshot.jpg
  ステータス (JSON): http://192.168.1.23:8080/api/status
```

初回起動時に Windows ファイアウォールの許可ダイアログが出た場合は「アクセスを許可する」を選んでください。

Web カメラを 2 台以上つないでいる場合は、[複数のカメラを使う](#複数のカメラを使う) を参照してください(既定では 1 台目だけを配信します)。

LAN の外(外出先のスマホなど)から見たい場合は、[LAN の外からアクセスする](#lan-の外からアクセスする-fido2-認証) を参照してください。

## 主なオプション

`start.bat` の後ろに付けるか、`python -m kasumin` に渡します。`--help` で全一覧が見られます。

| オプション | 既定値 | 説明 |
| --- | --- | --- |
| `--port`, `-p` | `8080` | 待ち受けポート |
| `--host` | `0.0.0.0` | 待ち受けアドレス。自分の PC からだけ見るなら `127.0.0.1` |
| `--camera`, `-c` | `0` | 配信するカメラの番号。`--list-cameras` で確認。複数台は繰り返して指定([複数のカメラを使う](#複数のカメラを使う)) |
| `--width` / `--height` | `1280` / `720` | 要求する解像度。`0` でカメラの既定値 |
| `--fps` | `30` | 要求するフレームレート |
| `--quality`, `-q` | `80` | JPEG 品質 (1〜100)。下げると帯域と CPU を節約できる |
| `--password` | なし | 指定すると Basic 認証が有効になる(ユーザー名は `--user`、既定 `admin`)。LAN 内向け |
| `--rotate` | `0` | 回転角度 (0 / 90 / 180 / 270) |
| `--flip` | `none` | 反転 (`h` 左右 / `v` 上下 / `hv` 両方) |
| `--timestamp` | off | 映像の左下に日時を焼き込む |
| `--idle-timeout SEC` | `0` | 視聴者がいない状態が SEC 秒続いたらカメラを解放する(LED が消える) |
| `--backend` | `dshow` | OpenCV のキャプチャバックエンド (`dshow` / `msmf` / `any`) |
| `--fourcc` | なし | カメラに要求するピクセル形式。高解像度で fps が出ないときは `MJPG` を試す |
| `--list-cameras` | | 利用できるカメラを表示して終了 |

LAN の外からのアクセス用:

| オプション | 既定値 | 説明 |
| --- | --- | --- |
| `--public-url URL` | なし | LAN の外から開くときの URL (例: `https://cam.example.com`)。指定すると FIDO2 認証が有効になる |
| `--tls-cert` / `--tls-key` | なし | Kasumin 自身が HTTPS で待ち受けるための証明書と秘密鍵 (PEM)。更新されると自動で読み込み直す |
| `--setup-link` | | キーが登録済みでも、新しいキーの登録リンクを表示する(キーをなくしたときなど) |
| `--session-days` | `7` | ブラウザのログイン状態を保つ日数 |
| `--user-verification` | `required` | キーの PIN / 生体認証を必須にするか。`preferred` にすると PIN 未設定のセキュリティキーでもログインできる |
| `--data-dir` | `%LOCALAPPDATA%\Kasumin` | 登録済みのキーとアクセストークン (`auth.json`)、設定画面で変えたカメラの名前と回転 (`cameras.json`) の保存先。複数の Kasumin を同時に起動するときは分ける |

例: 2 台目のカメラを 720p / 15fps、パスワード付きで 9000 番ポートに公開する

```bat
start.bat --camera 1 --fps 15 --port 9000 --password himitsu --timestamp
```

パスワードは環境変数 `KASUMIN_PASSWORD`(ユーザー名は `KASUMIN_USER`)でも渡せます。

## 複数のカメラを使う

既定では 1 台目(番号 0)のカメラだけを配信します。ビデオ会議用のカメラや OBS などの仮想カメラが
意図せず公開されないよう、2 台目以降は配信するカメラを指定したときだけ有効になります。
起動時にほかのカメラが見つかると、その番号と名前が表示されます。

まず、つながっているカメラの番号と名前を確認します。

```bat
start.bat --list-cameras
```

```
  [0] Integrated Webcam  1280x720 @ 30 fps
  [1] HD Pro Webcam C920  1280x720 @ 30 fps
```

配信するカメラを `--camera` で並べるか、`--camera all` で接続中のカメラをすべて配信します。

```bat
start.bat --camera 0 --camera 1
start.bat --camera all
```

カメラごとに名前や向き・解像度を変えたいときは、番号のあとに `キー=値` をカンマで続けます(空白を含む場合は `"` で囲みます)。
指定しなかった項目は `--width` `--rotate` などの全体の設定に従います。

```bat
start.bat --camera "0,name=リビング" --camera "1,name=玄関,rotate=180,width=640,height=480" --timestamp
```

| キー | 内容 |
| --- | --- |
| `name` | 画面やログに出す名前(既定は Windows に登録されているカメラ名) |
| `width` / `height` / `fps` | 要求する解像度とフレームレート |
| `rotate` / `flip` | 回転 (0 / 90 / 180 / 270) と反転 (`none` / `h` / `v` / `hv`) |
| `fourcc` | カメラに要求するピクセル形式(例: `MJPG`) |
| `quality` | JPEG 品質 (1〜100) |

FIDO2 認証 (`--public-url`) を使っている場合は、カメラの **名前と回転** をブラウザの「設定」画面からも変えられます
([設定画面でできること](#設定画面でできること) を参照)。

ブラウザでは画面上部のタブで「すべて」(一覧)と各カメラを切り替えます。一覧のカメラをクリックすると大きく表示します。
アプリからは `/camera/<番号>/stream` のように番号付きの URL で各カメラを開きます(番号なしの `/stream` は 1 台目)。

- Windows ではカメラを抜き差しすると番号が振り直されることがあります。複数台を配信しているときは、起動時のカメラを接続先 (USB のポートなど) で覚えているので、抜き差ししても同じ名前・同じ URL に同じカメラが映ります(抜いている間は NO SIGNAL)
- 起動後に新しくつないだカメラは自動では追加されません。Kasumin を起動し直してください
- 一覧表示では通信量を抑えるため、各カメラを最大 10 fps で表示します。5 台以上を HTTP (LAN 内) で一覧表示するときは、ブラウザの同時接続数の上限を避けるため、約 1 秒ごとの静止画の更新になります
- USB の Web カメラを何台も同時に使うと USB の帯域が足りなくなることがあります。下の [トラブルシューティング](#トラブルシューティング) を参照してください

## LAN の外からアクセスする (FIDO2 認証)

`--public-url` を指定すると、パスワードの代わりに **FIDO2 (WebAuthn)** で認証するようになります。
ログインに使えるのは次のような「キー」です。パスワードがないので総当たりや使い回しの心配がなく、
偽サイトに誘導されてもキーが反応しないためフィッシングにも強い方式です。

- **パスキー**: iPhone / Android / Windows Hello / Mac の Touch ID など、端末の生体認証や PIN でログイン
- **セキュリティキー**: YubiKey などの FIDO2 対応キー(既定では PIN の設定が必要)

ブラウザの仕組み上、FIDO2 には **ドメイン名の付いた HTTPS の URL** が必要です。
IP アドレス (`https://192.168.1.23`) や自己署名証明書では使えないので、次のいずれかの方法で公開 URL を用意してください。

### 方法 1: Tailscale (おすすめ)

ルーターの設定もドメインの取得も不要で、`https://<PC名>.<tailnet名>.ts.net` という URL と正規の証明書が自動で用意されます。

1. [Tailscale](https://tailscale.com/download) を Kasumin の PC と見る側のスマホなどにインストールし、同じアカウントでログインする
2. Kasumin の PC で次のどちらかを実行する(初回は有効化のための URL が表示されるので、指示に従ってください)
   ```bat
   rem Tailscale を入れた自分の端末からだけ見られるようにする (おすすめ)
   tailscale serve --bg 8080
   rem インターネット全体に公開する (Tailscale を入れていない端末からも見られる)
   tailscale funnel --bg 8080
   ```
3. 表示された URL を `--public-url` に指定して起動する。Tailscale 経由でだけ受け付けるよう `--host 127.0.0.1` も付けます
   ```bat
   start.bat --host 127.0.0.1 --public-url https://mypc.tail1234.ts.net
   ```

### 方法 2: Cloudflare Tunnel (独自ドメインがある場合)

1. ドメインを Cloudflare で管理し、Cloudflare Zero Trust のダッシュボードでトンネルを作成して、表示される手順で PC に `cloudflared` をインストールする
2. トンネルの公開ホスト名(例: `cam.example.com`)の転送先を `http://localhost:8080` にする
3. 起動する
   ```bat
   start.bat --host 127.0.0.1 --public-url https://cam.example.com
   ```

起動のたびに URL が変わる Quick Tunnel (`*.trycloudflare.com`) は使えません。パスキーは URL のホスト名ごとに登録されるためです。

### 方法 3: ルーターのポート開放 + Kasumin 自身の HTTPS

DDNS などで PC のグローバル IP にドメイン名を向け、Let's Encrypt などの証明書(win-acme なら PEM 形式で出力)を用意します。
ルーターで外部のポート(例: 443)を PC の 8443 番に転送し、証明書を指定して起動します。

```bat
start.bat --port 8443 --tls-cert C:\certs\cam-fullchain.pem --tls-key C:\certs\cam-key.pem --public-url https://cam.example.com
```

証明書が更新されると、再起動しなくても次の接続から新しい証明書を使います。
この方法では Kasumin がインターネットに直接さらされるので、できれば方法 1・2 を使ってください。

### 最初のキーを登録する

キーが 1 つも登録されていない状態で起動すると、登録リンクが表示されます。

```
  【キーの登録】次の URL をブラウザで開いて、パスキーまたはセキュリティキーを登録してください
  (21:30 まで有効・1 回限り。この URL は他の人に見せないでください)
    https://mypc.tail1234.ts.net/setup#t=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

このリンクをログインに使いたい端末(スマホなど)で開き、キーに名前を付けて「パスキー / セキュリティキーを登録」を押します。
PC のブラウザで開いた場合も、登録画面で「スマートフォンを使用」などを選べば、QR コード経由でスマホにパスキーを作れます。
登録が終わるとそのままログインした状態になります。登録リンクは 1 回使うと無効になります。

### 設定画面でできること

ログイン後、画面右上の「設定」から次の操作ができます。

- **カメラの名前と回転**: 一覧やタブ・ログに表示する名前と、映像の向き (時計回りに 90° / 180° / 270°) を変える。
  プレビューで向きを確かめながら変更でき、すぐに映像に反映される。値は `cameras.json` に保存され、次回の起動時も使われる
  (起動オプションの `--camera "1,name=…"` や `--rotate` より優先。「既定に戻す」で起動オプションの値やカメラの機種名に戻る)。
  カメラは接続先で覚えるので、番号が入れ替わっても別のカメラに設定が付くことはない
- **この端末でキーを追加** / **別の端末用の登録リンクを作成**(15 分間有効・1 回限り): 家族のスマホや予備のセキュリティキーを登録する
- **キーの削除**: なくした端末のキーを削除すると、そのキーでログインしていたセッションも切断される
- **アクセストークン**: VLC・OBS・Home Assistant などのアプリ用(次の節)
- **ログイン中のセッション**: どこからログインしているかの確認と、強制ログアウト

キーをすべてなくした場合は、Kasumin の PC で `--setup-link` を付けて起動すると新しい登録リンクが表示されます。
登録データ (`%LOCALAPPDATA%\Kasumin\auth.json`) を削除すると、キーとアクセストークンがすべて消えて最初の状態に戻ります。

### アプリから使う (アクセストークン)

パスキーでログインできないアプリには、「設定」で発行するアクセストークンを使います。
トークンで取得できるのはカメラの映像(ストリーム・スナップショット)と状態だけで、設定は変更できません。
アプリごとに発行しておけば、使わなくなったものだけ削除できます。

- URL に付ける: `https://mypc.tail1234.ts.net/stream?token=ksm_xxxx`
- ユーザー名とパスワードを入力するアプリ: ユーザー名は任意、パスワードにトークン
- HTTP ヘッダー: `Authorization: Bearer ksm_xxxx`

トークンは発行した画面でしか表示されません(Kasumin にはハッシュしか保存されません)。

## エンドポイント

| パス | 内容 |
| --- | --- |
| `/` | ブラウザ用の視聴ページ |
| `/stream` | MJPEG ストリーム (`multipart/x-mixed-replace`)。`?fps=5` のように配信レートを制限できる |
| `/snapshot.jpg` | 最新フレームの JPEG 1 枚 |
| `/api/status` | 解像度・実測 fps・視聴者数・稼働時間などの JSON。`cameras` に全カメラの状態が入る |
| `/camera/<番号>/stream` など | 指定したカメラの映像と状態。`/camera/1/snapshot.jpg` `/camera/1/api/status` のように上の各パスに番号を付ける |
| `/login` `/setup` `/settings` | FIDO2 認証のログイン・キー登録・設定ページ(`--public-url` 指定時のみ) |

番号なしの `/stream` `/snapshot.jpg` は 1 台目のカメラです(1 台だけのときと同じ URL で使えます)。
`/stream.mjpg` `/video` `/snapshot` `/image.jpg` などの別名も用意しています(IP カメラ向けアプリの既定パスに合わせやすいように)。

## 他のアプリから使う

- **VLC**: メディア → ネットワークストリームを開く → `http://<IP>:8080/stream`
- **OBS**: メディアソースを追加 → 「ローカルファイル」のチェックを外し、入力に `http://<IP>:8080/stream`
- **ffmpeg**: `ffmpeg -i http://<IP>:8080/stream -c:v copy out.mkv`(再エンコードなしで録画)
- **Home Assistant**: MJPEG IP Camera 統合で `mjpeg_url` に `/stream`、`still_image_url` に `/snapshot.jpg`
- **Python / OpenCV**: `cv2.VideoCapture("http://<IP>:8080/stream")`
- 2 台目以降のカメラは `http://<IP>:8080/camera/1/stream` のように番号を付けます(Home Assistant ならカメラごとに統合を追加)
- Basic 認証を有効にした場合は `http://admin:password@<IP>:8080/stream` の形式で指定します
- FIDO2 認証を有効にした場合は、公開 URL にアクセストークンを付けて `https://cam.example.com/stream?token=ksm_xxxx` の形式で指定します

## ログオン時に自動起動する

Windows のタスク スケジューラに登録すると、サインインしたときに Kasumin がコンソール画面を出さずに起動します。
登録する前に、次の 2 つを済ませておいてください。

- 一度 `start.bat` をコンソールで実行し、問題なく起動することを確かめる(依存パッケージのインストールもこのとき行われます)。
  FIDO2 認証を使う場合は、このときに最初のキーも登録しておきます(コンソールを出さない起動では登録リンクが見えないため)
- 確認に使ったコンソールの Kasumin は Ctrl+C で終了させる(二重に起動すると、ポートとキーの保存先が使用中で 2 つ目が起動できません)

PowerShell (管理者権限は不要) で次を実行します。`C:\path\to\Kasumin` は Kasumin を置いたフォルダに、
`-Argument` の中は `start.bat` の後ろに付けていたオプションに置き換えてください。

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\Kasumin\.venv\Scripts\pythonw.exe" -Argument "-m kasumin --host 127.0.0.1 --public-url https://cam.example.com --camera 0 --camera 1 --timestamp" -WorkingDirectory "C:\path\to\Kasumin"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Kasumin" -Action $action -Trigger $trigger -Settings $settings -Description "Kasumin ネットワークカメラ"
```

| 設定 | 理由 |
| --- | --- |
| `pythonw.exe` | コンソール画面を出さない Python。`start.bat` 経由だとコンソールが出るので直接起動する |
| `-WorkingDirectory` | `-m kasumin` で Kasumin を読み込むために、Kasumin のフォルダで起動する |
| `-ExecutionTimeLimit 0` | タスク スケジューラは既定で「3 日を超えたら停止」なので、上限をなくす(`schtasks /Create` で登録するとこの上限が付いたままになる) |
| `-AllowStartIfOnBatteries` / `-DontStopIfGoingOnBatteries` | ノート PC がバッテリー駆動のときも起動し、途中で止めない |
| `-RestartCount 3 -RestartInterval 1分` | タスクの開始に失敗したとき、1 分おきに 3 回まで試し直す |

登録したら、サインインし直さなくてもその場で起動して確かめられます。

```powershell
Start-ScheduledTask -TaskName "Kasumin"
```

止める・登録を削除するには次のようにします。

```powershell
Stop-ScheduledTask -TaskName "Kasumin"
Unregister-ScheduledTask -TaskName "Kasumin" -Confirm:$false
```

注意:

- 起動するのは Windows にサインインしたときです。停電などで PC が再起動したときも、サインインするまで配信されません
- コンソールを出さない起動ではログが表示されません。動かないときは、タスクを止めてから `start.bat` を同じオプションでコンソール実行し、表示されるメッセージを確認してください
- Kasumin を更新したあとは、一度 `start.bat` をコンソールで実行してください(依存パッケージが変わっていた場合にインストールされます)
- キーをなくして登録リンクを表示したいときは、タスクを止めてからコンソールで `start.bat ... --setup-link` を実行します
- Cloudflare Tunnel などを使っている場合は、そちらも自動起動しているか確認してください(`cloudflared` をサービスとしてインストールしていれば自動で起動します)

## セキュリティについて

- `--public-url` を指定しない場合、通信は暗号化されません(HTTP)。信頼できる LAN 内での利用を想定しています
  - LAN 内でも、他の人が見られる状況なら `--password` を付けることを推奨します
  - 自分の PC からだけ使うなら `--host 127.0.0.1` で外部からの接続を遮断できます
- LAN の外から見るときは、必ず `--public-url` (FIDO2 認証) と HTTPS を使ってください。パスワード (Basic 認証) のままルーターのポートを開けて公開するのは危険です
- FIDO2 認証では次のように保護しています
  - ログインは登録済みのキーの署名でだけ行い、既定ではキーの PIN / 生体認証も必須。署名カウンタが戻ったキー(複製の疑い)は拒否する
  - ログイン状態の Cookie は `HttpOnly`・`Secure`・`SameSite=Strict`。セッションはメモリ上にだけ持ち、再起動・ログアウト・キー削除で無効になる
  - 設定を変える操作は、公開 URL のページから送られたもの (Origin ヘッダー) だけを受け付ける
  - ページには Content-Security-Policy を付け、外部のスクリプトの実行や他サイトへの埋め込みを禁止している
  - 配信中のストリームも定期的に認証を確かめ直し、ログアウトやトークン削除の数秒後に止まる
  - アクセストークンと登録リンクは十分に長い乱数で、保存するのはハッシュだけ。`?token=` の値はログに残さない
- 登録リンクとアクセストークンは、知っている人なら誰でも使えます。人に見せたりチャットに貼ったりしないでください
- トンネル / リバースプロキシを使うときは `--host 127.0.0.1` を付け、LAN から HTTP で直接つながらないようにしてください

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

**複数のカメラを使うと、2 台目以降が NO SIGNAL のまま / カクつく**

- USB の帯域不足のことがよくあります。`--fourcc MJPG` を付ける(カメラ側で圧縮して送るので帯域が大幅に減る)か、`--width 640 --height 480` のように解像度を下げてください
- 同じ USB ハブにまとめず、PC 本体の別の USB ポート(できれば別のコントローラ)につなぐと改善することがあります
- `start.bat --list-cameras` で全カメラが「開けません」になっていないか確認してください

**カメラの名前と映像が入れ替わった**

- 2 台以上を配信しているときは抜き差しで番号がずれても同じカメラを追いかけますが、別の USB ポートにつなぎ直すと別の機器として扱われることがあります。Kasumin を起動し直してください

**「パスキーは公開 URL でしか使えません」と表示される**

- `http://192.168.1.23:8080/` のように公開 URL 以外で開いています。表示されたリンク(`--public-url` の URL)から開き直してください

**キーの操作がキャンセルされた・時間切れと表示される**

- キーの操作画面を閉じた、または 2 分以内に操作しなかった場合に表示されます。もう一度ボタンを押してください
- 登録していないキー(別の端末のパスキーなど)を選んだ場合も同じ表示になります。「設定」で登録済みのキーを確認してください

**再起動したらログアウトされた**

- 仕様です。ログイン状態はメモリ上にだけ保存しているので、Kasumin を再起動するとログインし直しになります(キーとアクセストークンはそのまま使えます)

## 開発

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

テストは合成フレームとソフトウェアで実装した FIDO2 認証器を使うので、カメラやセキュリティキーがない環境でも実行できます。

構成:

```
kasumin/
  camera.py     カメラのキャプチャスレッド、JPEG エンコードの共有、再接続
  server.py     HTTP サーバー (MJPEG ストリーム、スナップショット、ステータス、認証、HTTPS)
  auth.py       FIDO2 認証 (キーの登録・ログイン、セッション、アクセストークン)
  tls.py        HTTPS の証明書の読み込みと、更新時の自動再読み込み
  preferences.py  設定画面で変えたカメラの名前と回転の保存
  storage.py    設定ファイルの書き込み (書き込み途中で壊れないように置き換える)
  devices.py    カメラデバイスの探索 (DirectShow でのカメラ名・接続先の列挙)
  cli.py        コマンドライン引数 (カメラの指定の解釈を含む) と起動処理
  static/       ブラウザ用のページ (視聴・ログイン・設定) と共通の CSS / JavaScript
tests/          サーバー・カメラ管理・FIDO2 認証の結合テストと、カメラの指定の解釈のテスト
```
