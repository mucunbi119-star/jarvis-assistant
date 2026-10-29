# 外部サービスの仕様を確認した資料

確認日: 2026-10-28（日本時間）。料金額は断定せず、利用前に各サービスの公式料金・契約をご確認ください。

## OpenAI API

以下は OpenAI が公開する公式 Python SDK の `main` から、実装時に HTTP 200 で取得した資料です。API 自体は HTTP クライアントで呼び出しています。

- [公式 SDK README](https://github.com/openai/openai-python/blob/main/README.md): Responses API の `model`、`instructions`、`input` とサーバー側 API キー管理。
- [公式 WebSearchToolParam](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/web_search_tool_param.py): `web_search`、日本・タイムゾーン設定。Responses API の検索ツールを使い、Chrome 操作とは分離しました。
- [公式 ResponseOutputText](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_output_text.py): `url_citation` のタイトル・URL を利用。モデルが文章中に生成しただけのリンクを検証済み出典として登録しません。
- [公式構造化出力の設定](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_format_text_json_schema_config_param.py): 回答を表に変換する `json_schema` 出力。受信後もローカルで表の形を検証します。
- [Responses API ガイド](https://developers.openai.com/api/docs/guides/text)、[Web 検索](https://developers.openai.com/api/docs/guides/tools-web-search)、[API 料金](https://openai.com/api/pricing/): 利用前の参照先。開発環境のネットワーク制限により、これらのサイト本体は取得できず、上記公式 SDK の現行ソースを確認しました。

`gpt-4.1-mini` を初期モデル名にしています。利用権限・検索対応はアカウントによって異なり、`OPENAI_MODEL` で変更できます。API キー未提供のため、有料 API の実応答・実検索・実課金は未確認です。HTTP 仕様のテストとデモの検証を実サービス検証と混同していません。

## ChatGPT の Web 画面

- [OpenAI 利用規約](https://openai.com/policies/terms-of-use/)
- [OpenAI サービス利用規約](https://openai.com/policies/service-terms/)

現行の規約本文と公式な Web 画面自動操作連携の可否は、この実行環境から確認できませんでした。このため、Web 画面の自動質問送信・回答抽出は実装していません。専用 Chrome で開き、表示した文章をユーザー自身がコピー・貼り付けして送信する方式です。ログインやアクセス制限の回避は行いません。API と ChatGPT の契約・課金は別に扱います。

Google と Chrome 操作の公式資料は [INTEGRATIONS.md](INTEGRATIONS.md) に記録しています。

## ブラウザ音声

- [Web Speech API 公式仕様ソース](https://github.com/WebAudio/web-speech-api/blob/main/index.bs): 実装時に HTTP 200 で取得し、`getVoices`、`localService`、`rate`、`volume`、録音停止 API を確認しました。
- [Web Speech API（MDN）](https://developer.mozilla.org/ja/docs/Web/API/Web_Speech_API)
- [Microsoft Edge の音声認識 API に関する公式説明](https://learn.microsoft.com/microsoft-edge/web-platform/web-speech-api)

ブラウザが実際に返す日本語対応の `SpeechSynthesisVoice` だけを表示します。声の一覧は Windows・ブラウザに依存し、すべての環境で同じ声を保証しません。`localService` が偽の音声はネットワークサービスの可能性を表示します。音声認識は Chrome 等のブラウザ実装に依存し、音声がブラウザ提供者のサーバーへ送信される場合があります。マイク利用前に案内します。音声機能自体にこのアプリから OpenAI 音声 API を呼ぶ処理はありません。上記音声サイト本文のネットワーク取得・Windows 音声の実機検証は未実施です。

## WindowsでのPython準備（2026-10-29追記）

[Python公式Windowsガイドの現行ソース](https://github.com/python/cpython/blob/main/Doc/using/windows.rst)を実際に取得しました。新旧ランチャーの`py -0p`互換と、Python install managerが管理ツールとランタイムを分けて扱うこと、`PYTHON_MANAGER_AUTOMATIC_INSTALL`で検出中の自動インストールを止められることを確認しました。配布バッチは既存のPythonだけを検出し、インストーラーは自動実行しません。公式ダウンロード先は https://www.python.org/downloads/windows/ です。
