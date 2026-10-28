# Chrome・Google・Windows との連携

このページは **2026年10月28日** 時点の実装を説明します。Windows と外部アカウントを使う実機確認は、この Linux クラウド環境では行っていません。

## 専用 Chrome

Windows に通常の Google Chrome をインストールしてください。アシスタントは `Program Files`、`Program Files (x86)`、`LOCALAPPDATA` の標準インストール先を調べます。`playwright` パッケージは必要ですが、別の Chromium のダウンロードは不要です。

起動すると `data/chrome-profile`（保存先を変更した場合はその配下）に、このアシスタント専用プロファイルを作ります。普段の Chrome の履歴・保存パスワードは読みません。専用 Chrome のログイン状態はこのプロファイルに残ることがあるため、端末を共有しないでください。ログイン・追加認証・アクセス制限への対応はご自身で行ってください。

利用可能な操作は、起動、公開 HTTP/HTTPS ページを開く、Google でキーワード検索、現在のページ本文の取得、停止です。停止すると専用 Chrome を閉じます。送信済みの通信は取り消せません。本文取得は実際の DOM のテキストで、要約には別途 AI の会話機能を使います。読み取れないページはエラーとして表示します。ローカル・プライベートネットワークへの移動は許可しません。

### ChatGPT

「ChatGPT を開く」は、確認後に `https://chatgpt.com/` を開くだけです。質問文は自動入力・送信しません。画面で質問を確認し、ご自身でコピーして送信してください。回答の自動取得も未対応です。

ChatGPT Web の自動操作について、実装時点の利用規約をこの環境から直接確認できませんでした。そのため許可されているとは判断せず、自動送受信を実装していません。OpenAI API によるアシスタント自身の応答とは別機能です。ChatGPT の契約で API 料金が支払われるわけではありません。

## Google スプレッドシート

Google 未接続でも、ローカルの Excel・CSV 出力を使えます。

1. Google Cloud コンソールで、ご自身のプロジェクトを用意します。自動で契約・課金登録は行いません。
2. Google Sheets API を有効にします。
3. OAuth 同意画面を設定します。テスト状態ではご自身をテストユーザーに追加します。
4. OAuth クライアントを **デスクトップアプリ** として作成し、JSON をダウンロードします。Web アプリ用 JSON は使用しません。
5. JSON を他人と共有しない場所に置きます。リポジトリに追加しないでください。
6. `.env` の `GOOGLE_CLIENT_SECRETS_FILE` に JSON の絶対パスを指定して、アシスタントを再起動します。
7. 設定の Google 接続で、接続先と権限の説明を読んで接続します。ブラウザでご自身の Google アカウントにログインし、同意します。待ち時間は120秒です。

設定例（秘密の値は含めません）:

```dotenv
GOOGLE_CLIENT_SECRETS_FILE=C:/Users/あなたの名前/Documents/jarvis-private/google-client.json
```

認証コールバックは `127.0.0.1` のランダムなローカルポートで待ち受けます。外部公開サーバーは使いません。OAuth の PKCE を利用します。認証画面が開かない場合は、既定ブラウザ、Windows ファイアウォール、デスクトップ用クライアント設定を確認してください。認証コード・パスワードをチャットへ貼らないでください。

権限は `https://www.googleapis.com/auth/drive.file` のみです。このアプリが作成したファイルだけを扱う設計です。任意の既存ファイルを選択して許可する Google Drive Picker は未実装です。そのため、現在は「このアプリで新規作成したシート」の読み込みと更新に限定しています。アカウント上の全ファイルを扱う広い権限を要求しません。

表を送信するときは、タイトル、列名、全ての送信行、更新の場合はシート ID と範囲を確認してください。Google に送られるのはその表データとタイトル・範囲です。Google Sheets API の `RAW` 入力を使うため、外部由来の `=...` を数式として実行しません。シート作成後にデータ送信だけ失敗する場合は、シートが既に存在することをエラーに表示します。重複作成せず、既存の対象への更新を再試行してください。

「接続済み」はローカルにトークンがある状態を示します。Google 側で失効している場合などは操作時にエラーになり、再接続が必要です。シートの更新では指定した値の範囲を上書きします。送った範囲外の古いセルは消しません。

認証トークンは `data/credentials/google-token.json` に保存します。Windows では自身のユーザーだけが読めるフォルダにアプリを置いてください。アシスタントの「接続解除」はこのローカルトークンを削除し、進行中の認証結果も破棄します。Google 側の許可は、Google アカウントの「サードパーティとの接続」から別途取り消せます。作成済みスプレッドシートは接続解除で削除しません。

API 利用上限や費用は公式の現行情報を確認してください。このアプリは金額を断定せず、有料契約を自動で行いません。

## 登録したアプリ・フォルダ

起動対象は、ご自身が確認して登録した絶対パスの `.exe` またはフォルダだけです。ネットワーク共有、スクリプト、ショートカット、コマンド文字列、追加引数は登録できません。AI が作った文字列をシェルで実行する仕組みはありません。登録の削除はアプリ・フォルダ本体を削除しません。

実行結果は「Windows に起動を依頼しました」と表示します。編集ソフト内部の起動完了や作業の自動操作は検証していません。素材の自動移動や編集ソフトの高度な操作は未実装です。

## 実装時に確認した公式のソース

以下は上記日付に HTTPS で取得し、該当 API 定義を確認しました。Web サイトの利用規約確認とは区別しています。

- [Google 公式 Sheets API discovery](https://github.com/googleapis/google-api-python-client/blob/main/googleapiclient/discovery_cache/documents/sheets.v4.json): `spreadsheets.create`、`get`、`values.update` が `drive.file` を許可すること。
- [Google 公式 OAuth flow.py](https://github.com/googleapis/google-cloud-python/blob/main/packages/google-auth-oauthlib/google_auth_oauthlib/flow.py): `InstalledAppFlow.run_local_server` の `bind_addr`、`timeout_seconds`、PKCE 対応。パッケージの公式リポジトリは PyPI の現行メタデータから確認しました。
- [Playwright 公式 Python API](https://github.com/microsoft/playwright-python/blob/main/playwright/async_api/_generated.py): 永続プロファイルと Chrome の起動パス指定に対応すること。

`developers.google.com`、`playwright.dev`、`openai.com/policies/terms-of-use/` への直接取得は、このクラウド環境のネットワーク設定で HTTP 403 となりました。現行の Web 利用規約を確認済みとは記載していません。
