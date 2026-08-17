# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

日本語でコメントを出力する。

package.jsonやrequirements.txtに記載された依存関係が未インストールの場合は自動でインストールする。

修正を行う際は、該当修正によって他の処理に問題がないか慎重に確認を行って作業を行ってください。

言語の指定がない場合は、全てpythonで生成してください。

ファイルの作成・上書きは確認せずに実行してください。

コードを生成するためのプロンプト、実行内容や、エラーが起きた場合のメッセージや試行内容、追加した機能など詳しい記録を"記録.txt"で都度行う。ファイルがない場合はこのファイルと同じ場所に新規で作成する。

# プロジェクト構成ルール
機能ごとにファイルを分割すること。各ファイルの役割は以下の通り：
- main.py：Webhookエントリーポイント・APScheduler起動
- line_service.py：LINE送受信・Quick Reply
- shift_parser.py：OpenAIによる自然言語解析
- calendar_service.py：Google Calendar連携
- sheets_service.py：Google Sheets連携
- salary_calculator.py：勤務時間・給与計算
- notification.py：前日通知ロジック
- user_settings.py：ユーザー設定管理
- config.py：環境変数・定数管理