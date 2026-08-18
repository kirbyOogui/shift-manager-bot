"""main.py / handlers 配下の各ハンドラーが共有するインメモリ状態と共通定数。

Webhookの1リクエストごとに使い捨てではなく、ユーザーの次の発言・ボタン操作を
待つための「確認待ち」状態をプロセスメモリ上に保持する。複数ハンドラーファイルから
参照されるため、循環importを避けるためにこのモジュールへ集約している。
"""

import json

# 複数シフト一括登録の確認待ちデータを一時保存
pending_multi_shifts: dict = {}
# 控除データの確認待ちデータを一時保存
pending_deductions: dict = {}
# 手当削除の確認待ち
pending_del_allowance: dict = {}
# 控除項目削除の確認待ち
pending_del_custom_deduction: dict = {}
# シフト修正の確認待ち
pending_update_shifts: dict = {}
# 有給使用の確認待ち
pending_leave_usage: dict = {}
# 有給削除・修正の確認待ち
pending_delete_leave: dict = {}
pending_modify_leave: dict = {}
# 控除データ削除の確認待ち
pending_delete_deductions: dict = {}
# ヘルプキーワード検索モード中のユーザー
help_mode: set = set()
# 名前登録の入力待ち状態
name_input_mode: set = set()
# 設定変更の入力待ち状態 {user_id: setting_type}
setting_input_mode: dict[str, str] = {}
# 有給付与日数の入力待ち状態（ボタン押下後、数字だけ送れば完結する）
leave_grant_input_mode: set = set()
# 明細から読み取った手当の登録待ち {user_id: {allowances, profile_name}}
pending_payslip_allowances: dict = {}
# 明細から読み取った未登録の控除項目の登録待ち {user_id: {items, profile_name, allowances, work_days}}
pending_payslip_deductions: dict = {}
# シフト表の名前選択待ち {user_id: {image_result, detected_names}}
pending_name_selection: dict = {}
# カレンダー色の一括更新確認待ち {user_id: new_color_name}
pending_color_update: dict = {}

# リッチメニューボタン押下時のQuick Reply定義
RICH_MENU_REPLIES: dict = {
    "シフト管理": {
        "text": "📋 シフト管理\nどの操作をしますか？",
        "items": [
            ("📷 写真で登録", "写真でシフトを登録する"),
            ("📝 手動で登録", "シフトを登録したい"),
            ("📅 シフト一覧", "シフト一覧"),
            ("✏️ シフト修正", "シフトを修正したい"),
            ("🗑️ シフト削除", "シフトを削除したい"),
            {"label": "✕ 閉じる", "data": json.dumps({"action": "close_menu"})},
        ],
    },
    "給与・明細": {
        "text": "💴 給与・明細\nどの操作をしますか？",
        "items": [
            ("今月の給与", "今月の給与確認"),
            ("先月の給与", "先月の給与確認"),
            ("明細を登録", "明細を登録したい"),
            ("手当を確認", "手当一覧"),
            ("➕ 手当を追加", "手当を追加したい"),
            ("🗑️ 手当を削除", "手当を削除したい"),
            ("控除を確認", "控除一覧"),
            ("➕ 控除を追加", "控除を追加したい"),
            ("🗑️ 控除を削除", "控除を削除したい"),
            {"label": "✕ 閉じる", "data": json.dumps({"action": "close_menu"})},
        ],
    },
    "有給管理": {
        "text": "🌿 有給管理\nどの操作をしますか？",
        "items": [
            ("残日数確認", "有給残日数を確認"),
            ("📜 付与履歴", "有給付与履歴"),
            ("有給を取得", "有給を取りたい"),
            ("有給の付与", "有給が付与されました"),
            {"label": "⏰ 標準時間を設定", "data": json.dumps({"action": "setting_input_start", "setting": "leave_hours"})},
            {"label": "✕ 閉じる", "data": json.dumps({"action": "close_menu"})},
        ],
    },
    "仕事名": {
        "text": "💼 仕事名\nどの操作をしますか？",
        "items": [
            ("📝 名前を設定", "シフト表での名前を設定したい"),
            ("仕事名一覧", "仕事名一覧"),
            ("仕事名を切替", "プロファイルを切り替えたい"),
            ("➕ 仕事名を追加", "仕事名を追加したい"),
            ("🗑️ 仕事名を削除", "仕事名を削除したい"),
            {"label": "✕ 閉じる", "data": json.dumps({"action": "close_menu"})},
        ],
    },
}

SETTING_PROMPTS = {
    "hourly_wage":    ("💰 新しい時給（円）を入力してください。",    "例：1200"),
    "break_minutes":  ("⏰ 新しい休憩時間（分）を入力してください。", "例：60"),
    "notify_time":    ("🔔 通知時刻を入力してください。",            "例：18:00"),
    "calendar_title": ("📌 Googleカレンダーに表示する予定名を入力してください。", "例：シフト"),
    "leave_hours":    ("🌿 有給1日あたりの時間数を入力してください。", "例：8"),
}

SETTING_LABELS = {
    "hourly_wage":    "時給",
    "break_minutes":  "休憩時間",
    "notify_time":    "通知時刻",
    "calendar_title": "カレンダー予定名",
    "leave_hours":    "有給標準時間",
}

CALENDAR_COLORS = ["ブルーベリー", "トマト", "バジル", "バナナ", "グレープ", "フラミンゴ", "ミカン", "ピーコック", "グラファイト", "セージ", "ラベンダー"]

# OAuthコールバック完了後に表示するHTML
OAUTH_SUCCESS_HTML = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>連携完了</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#f0f9f0;}
h1{color:#27ae60;}p{color:#555;}</style></head>
<body><h1>✅ 連携完了！</h1>
<p>Googleカレンダーとの連携が完了しました。<br>LINEに戻ってシフトを送信してください。</p>
</body></html>
"""

OAUTH_ERROR_HTML = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>エラー</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#fdf0f0;}
h1{color:#e74c3c;}p{color:#555;}</style></head>
<body><h1>❌ 連携に失敗しました</h1>
<p>URLの有効期限が切れているか、認証に失敗しました。<br>LINEに戻って再度お試しください。</p>
</body></html>
"""
