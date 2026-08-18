import json
import logging
from datetime import datetime, timedelta

from flask import Flask, request, abort, redirect, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, ImageMessageContent
from apscheduler.schedulers.background import BackgroundScheduler

import config
import sheets_service
import shift_parser
import calendar_service
import salary_calculator
import user_settings
import notification
import line_service
import oauth_service
import flex_builder
import help_content

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
handler = WebhookHandler(config.LINE_CHANNEL_SECRET)

# 複数シフト一括登録の確認待ちデータを一時保存
_pending_multi_shifts: dict = {}
# 控除データの確認待ちデータを一時保存
_pending_deductions: dict = {}
# 手当削除の確認待ち
_pending_del_allowance: dict = {}
# 控除項目削除の確認待ち
_pending_del_custom_deduction: dict = {}
# シフト修正の確認待ち
_pending_update_shifts: dict = {}
# 有給使用の確認待ち
_pending_leave_usage: dict = {}
# 有給削除・修正の確認待ち
_pending_delete_leave: dict = {}
_pending_modify_leave: dict = {}
# 控除データ削除の確認待ち
_pending_delete_deductions: dict = {}
# ヘルプキーワード検索モード中のユーザー
_help_mode: set = set()
# 名前登録の入力待ち状態
_name_input_mode: set = set()
# 設定変更の入力待ち状態 {user_id: setting_type}
_setting_input_mode: dict[str, str] = {}
# 有給付与日数の入力待ち状態（ボタン押下後、数字だけ送れば完結する）
_leave_grant_input_mode: set = set()
# 明細から読み取った手当の登録待ち {user_id: {allowances, profile_name}}
_pending_payslip_allowances: dict = {}
# 明細から読み取った未登録の控除項目の登録待ち {user_id: {items, profile_name, allowances, work_days}}
_pending_payslip_deductions: dict = {}
# シフト表の名前選択待ち {user_id: {image_result, detected_names}}
_pending_name_selection: dict = {}
# カレンダー色の一括更新確認待ち {user_id: new_color_name}
_pending_color_update: dict = {}

# リッチメニューボタン押下時のQuick Reply定義
_RICH_MENU_REPLIES: dict = {
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

try:
    sheets_service.ensure_sheets()
    logger.info("Sheetsの初期化完了")
except Exception as e:
    logger.warning(f"Sheets初期化スキップ（環境変数未設定の可能性）: {e}")

# APScheduler: 毎分、前日通知チェックを実行
_scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
_scheduler.add_job(
    notification.check_and_notify,
    trigger="cron",
    minute="*",
    id="daily_notification",
    replace_existing=True,
)
# 毎朝8:00に締め日翌日チェック → 月次レポート送信
_scheduler.add_job(
    notification.check_and_report,
    trigger="cron",
    hour=8,
    minute=0,
    id="monthly_report",
    replace_existing=True,
)
_scheduler.start()

# OAuthコールバック完了後に表示するHTML
_OAUTH_SUCCESS_HTML = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>連携完了</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#f0f9f0;}
h1{color:#27ae60;}p{color:#555;}</style></head>
<body><h1>✅ 連携完了！</h1>
<p>Googleカレンダーとの連携が完了しました。<br>LINEに戻ってシフトを送信してください。</p>
</body></html>
"""

_OAUTH_ERROR_HTML = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>エラー</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#fdf0f0;}
h1{color:#e74c3c;}p{color:#555;}</style></head>
<body><h1>❌ 連携に失敗しました</h1>
<p>URLの有効期限が切れているか、認証に失敗しました。<br>LINEに戻って再度お試しください。</p>
</body></html>
"""


# ── ヘルスチェック ──────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "OK"


# ── Google OAuth ────────────────────────────────────
@app.route("/oauth/start", methods=["GET"])
def oauth_start():
    """LINEから開くOAuth開始エンドポイント。Googleの認証ページにリダイレクトする。"""
    user_id = request.args.get("user_id", "")
    if not user_id:
        return "invalid request", 400
    auth_url = oauth_service.generate_auth_url(user_id)
    return redirect(auth_url)


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Googleからのコールバックを受け取り、トークンを保存してLINEに通知する。"""
    state = request.args.get("state", "")
    code = request.args.get("code", "")

    user_id = oauth_service.handle_callback(state, code)
    if not user_id:
        return _OAUTH_ERROR_HTML, 400

    try:
        line_service.push_text(
            user_id,
            "✅ Googleカレンダーとの連携が完了しました！\n再度シフトを送信してください。"
        )
    except Exception as e:
        logger.error(f"連携完了通知の送信失敗: {e}")

    return _OAUTH_SUCCESS_HTML


# ── LINE Webhook ────────────────────────────────────
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "OK"
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token
    logger.info(f"[handle_message] user={user_id} text={text[:40]}")

    try:
        _dispatch_message(event, user_id, text, reply_token)
    except Exception as e:
        logger.error(f"[handle_message] 未補足エラー: {e}", exc_info=True)
        try:
            line_service.reply_text(reply_token, "エラーが発生しました。しばらく時間をおいて再度お試しください。")
        except Exception:
            pass


def _extract_employee_name(text: str) -> str:
    """自然な言い回しから従業員名だけを抽出する。"""
    import re
    name = text.strip()
    # 先頭の「名前は」「自分の名前は」などを除去
    name = re.sub(r'^(自分の名前は?|名前は?|シフト表での名前は?|名前を)', '', name).strip()
    # 末尾の「です」「にして」「に設定して」などを除去
    name = re.sub(
        r'(です|ます|にしてください|にして|に設定してください|に設定して|に設定|として登録してください|として登録|として|でお願いします|でお願い|でよろしく).*$',
        '', name
    ).strip()
    return name


def _parse_setting_input(setting_type: str, text: str):
    """設定変更入力モードでユーザーが送ったテキストを適切な値に変換する。"""
    import re
    t = text.strip()
    if setting_type == "hourly_wage":
        m = re.search(r'\d[\d,]*', t)
        return int(m.group().replace(',', '')) if m else None
    if setting_type == "break_minutes":
        m = re.search(r'(\d+)', t)
        if m:
            val = int(m.group(1))
            return val * 60 if '時間' in t and '分' not in t else val
        return None
    if setting_type == "notify_time":
        m = re.search(r'(\d{1,2})[時：:h](\d{0,2})', t)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2)) if m.group(2) else 0
            return f"{hh:02d}:{mm:02d}"
        return None
    if setting_type == "calendar_title":
        return t if t else None
    if setting_type == "leave_hours":
        m = re.search(r'\d+\.?\d*', t)
        return float(m.group()) if m else None
    return None


_SETTING_PROMPTS = {
    "hourly_wage":    ("💰 新しい時給（円）を入力してください。",    "例：1200"),
    "break_minutes":  ("⏰ 新しい休憩時間（分）を入力してください。", "例：60"),
    "notify_time":    ("🔔 通知時刻を入力してください。",            "例：18:00"),
    "calendar_title": ("📌 Googleカレンダーに表示する予定名を入力してください。", "例：シフト"),
    "leave_hours":    ("🌿 有給1日あたりの時間数を入力してください。", "例：8"),
}

_SETTING_LABELS = {
    "hourly_wage":    "時給",
    "break_minutes":  "休憩時間",
    "notify_time":    "通知時刻",
    "calendar_title": "カレンダー予定名",
    "leave_hours":    "有給標準時間",
}

_CALENDAR_COLORS = ["ブルーベリー", "トマト", "バジル", "バナナ", "グレープ", "フラミンゴ", "ミカン", "ピーコック", "グラファイト", "セージ", "ラベンダー"]


def _settings_qr() -> list[dict]:
    """設定変更ボタン一覧（共通クイックリプライ）。"""
    return [
        {"label": "💰 時給を変更",    "data": json.dumps({"action": "setting_input_start", "setting": "hourly_wage"})},
        {"label": "⏰ 休憩時間を変更", "data": json.dumps({"action": "setting_input_start", "setting": "break_minutes"})},
        {"label": "🔔 通知設定",      "data": json.dumps({"action": "setting_notify_menu"})},
        {"label": "🗓️ カレンダー設定", "data": json.dumps({"action": "setting_calendar_menu"})},
        {"label": "👤 名前を設定",    "data": json.dumps({"action": "setting_input_start", "setting": "employee_name"})},
        {"label": "🏥 社会保険",      "data": json.dumps({"action": "setting_social_menu"})},
    ]


def _dispatch_message(event, user_id: str, text: str, reply_token: str) -> None:
    # リッチメニューボタン押下：GPT不要で直接Quick Replyを返す
    if text in _RICH_MENU_REPLIES:
        menu = _RICH_MENU_REPLIES[text]
        line_service.reply_with_message_quickreply(reply_token, menu["text"], menu["items"])
        return

    # PC版LINEなどリッチメニューが表示されない環境向けのテキストコマンド：
    # GPT不要で、リッチメニューと同じ6カテゴリのQuick Replyを直接返す
    if text in ("メニュー", "menu", "Menu", "MENU"):
        items = [
            ("📋 シフト管理", "シフト管理"),
            ("💴 給与・明細", "給与・明細"),
            ("🌿 有給管理", "有給管理"),
            ("💼 仕事名", "仕事名"),
            {"label": "⚙️ 設定確認", "data": json.dumps({"action": "setting_show"})},
            {"label": "❓ ヘルプ", "data": json.dumps({"action": "menu_help"})},
        ]
        line_service.reply_with_message_quickreply(reply_token,
            "📱 メニュー\nどの操作をしますか？", items)
        return

    # 写真登録ガイド
    if text == "写真でシフトを登録する":
        gs = user_settings.get_settings(user_id)
        employee_name = gs.get("従業員名", "") or ""
        msg = (
            "📷 シフト表の写真を送るだけで自動登録できます！\n\n"
            "【手順】\n"
            "① このトーク画面でシフト表の写真を送信\n"
            "② 読み取り内容を確認\n"
            "③「登録する」をタップで完了\n\n"
            "※ 複数日まとめて登録できます\n\n"
            "📝 読み取り精度について\n"
            "・紙の写真より電子データ（アプリのスクリーンショット等）のほうが正確に読み取れます\n"
            "・読み取り後に内容を必ずご確認ください"
        )
        if not employee_name:
            msg += "\n\n💡 名前を登録するとあなたのシフトのみ自動抽出できます。\n「仕事名」→「名前を設定」から登録できます。"
        line_service.reply_text(reply_token, msg)
        return

    # 名前登録ガイド：GPTを介さず直接入力待ちにする（GPTが値を誤って埋めてしまうのを防ぐ）
    if text == "シフト表での名前を設定したい":
        _name_input_mode.add(user_id)
        line_service.reply_text(reply_token,
            "📝 シフト表に表示されているあなたの名前（フルネーム）を送ってください。\n\n"
            "例：山田太郎"
        )
        return

    # 仕事名追加ガイド
    if text == "仕事名を追加したい":
        line_service.reply_text(reply_token,
            "💼 追加したい仕事名の内容を教えてください。\n\n"
            "例：バイトBを追加して 9:00〜17:00 時給1200円 休憩60分 青\n\n"
            "【設定できる項目】\n"
            "・デフォルト時間（シフト登録で自動入力）\n・時給\n・休憩時間\n・カレンダーの色\n"
            "・締め日（例：25日）→ 月次レポートに使用\n・給料日（例：翌月10日）"
        )
        return

    # 仕事名削除ガイド：登録済みプロファイルをボタンで提示（タップで即座に削除確認へ）
    if text == "仕事名を削除したい":
        profiles = sheets_service.get_profiles(user_id)
        names = [p.get("プロファイル名", "") for p in profiles if p.get("プロファイル名")]
        if not names:
            line_service.reply_text(reply_token, "🗑️ まだ仕事名が登録されていません。")
            return
        items = [
            {"label": n[:20], "data": json.dumps({"action": "select_delete_profile", "name": n})}
            for n in names[:13]
        ]
        line_service.reply_with_quickreply(reply_token,
            "🗑️ 削除したい仕事名を選んでください。", items)
        return

    # 手当削除ガイド：登録済み手当をボタンで提示（タップで即座に削除確認へ）
    if text == "手当を削除したい":
        allowances = sheets_service.get_allowances(user_id)
        names = [a.get("手当名", "") for a in allowances if a.get("手当名")]
        if not names:
            line_service.reply_text(reply_token,
                "🗑️ カスタム手当が登録されていません。\n"
                "例：バイトリーダー手当を月5000円で追加して")
            return
        items = [
            {"label": n[:20], "data": json.dumps({"action": "select_delete_allowance", "name": n})}
            for n in names[:13]
        ]
        line_service.reply_with_quickreply(reply_token,
            "🗑️ 削除したい手当を選んでください。", items)
        return

    # 控除削除ガイド：登録済み控除をボタンで提示（タップで即座に削除確認へ）
    if text == "控除を削除したい":
        deductions = sheets_service.get_custom_deductions(user_id)
        names = [d.get("控除名", "") for d in deductions if d.get("控除名")]
        if not names:
            line_service.reply_text(reply_token,
                "🗑️ カスタム控除が登録されていません。\n"
                "例：組合費を固定で月1000円の控除として追加して")
            return
        items = [
            {"label": n[:20], "data": json.dumps({"action": "select_delete_custom_deduction", "name": n})}
            for n in names[:13]
        ]
        line_service.reply_with_quickreply(reply_token,
            "🗑️ 削除したい控除を選んでください。", items)
        return

    # 仕事名切り替えガイド：登録済みプロファイルをボタンで提示（タップで即座に切り替え）
    if text == "プロファイルを切り替えたい":
        profiles = sheets_service.get_profiles(user_id)
        names = [p.get("プロファイル名", "") for p in profiles if p.get("プロファイル名")]
        if not names:
            line_service.reply_text(reply_token,
                "🔄 まだ仕事名が登録されていません。\n先に仕事名を登録してください。\n"
                "例：バイトAという仕事名を時給1200円で登録して")
            return
        active = user_settings.get_settings(user_id).get("アクティブプロファイル", "") or ""
        if len(names) == 1:
            if active == names[0]:
                line_service.reply_text(reply_token,
                    f"🔄 現在登録されている仕事名は「{names[0]}」のみです。\n"
                    f"切り替え先を増やすには、新しい仕事名を登録してください。\n\n"
                    f"例：バイトBを追加して 9:00〜17:00 時給1200円 休憩60分")
                return
            # アクティブ設定が空・不整合な場合は選び直せるようにする
            line_service.reply_with_quickreply(reply_token,
                f"🔄 仕事名「{names[0]}」を選択しますか？",
                [{"label": names[0][:20], "data": json.dumps({"action": "switch_profile_direct", "name": names[0]})}]
            )
            return
        items = [
            {"label": f"{'✅ ' if n == active else ''}{n}"[:20],
             "data": json.dumps({"action": "switch_profile_direct", "name": n})}
            for n in names[:12]
        ]
        items.append(("➕ 新しく作る", "仕事名を追加したい"))
        line_service.reply_with_message_quickreply(reply_token,
            "🔄 切り替えたい仕事名を選んでください。", items)
        return

    # 有給付与ガイド：数字だけ送れば完結するよう入力待ち状態にする
    if text == "有給が付与されました":
        _leave_grant_input_mode.add(user_id)
        line_service.reply_text(reply_token, "📋 付与された有給の日数を数字で送ってください。\n\n例：10")
        return

    # Quick Reply 固定メッセージ：追加情報が必要な操作 → プロンプトを表示
    _QR_PROMPTS = {
        "シフトを登録したい": (
            "📝 登録したいシフトを教えてください。\n\n"
            "例：\n・7月15日 9:00〜18:00\n・7/15 9時〜18時\n\n"
            "📷 写真で一括登録もできます！\nシフト表の写真をそのまま送信してください。"
        ),
        "シフトを修正したい": (
            "✏️ 修正したいシフトの日付と新しい時間を教えてください。\n\n"
            "例：7月15日を10:00〜19:00に変更"
        ),
        "シフトを削除したい": (
            "🗑️ 削除したいシフトの日付を教えてください。\n\n"
            "例：7月15日のシフトを削除"
        ),
        "明細を登録したい": (
            "📄 給与明細の情報を教えてください。\n\n"
            "例：6月の明細 総支給15万 健保7755円 厚生年金13725円 雇用保険830円\n\n"
            "📷 明細の写真を送るだけでも自動で読み取れます！\n\n"
            "📝 読み取り精度について\n"
            "・紙の写真より電子データ（アプリのスクリーンショット等）のほうが正確に読み取れます\n"
            "・読み取り後に内容を必ずご確認ください"
        ),
        "手当を追加したい": (
            "🎁 追加したい手当の内容を教えてください。\n\n"
            "【月額固定】毎月一定額が加算\n"
            "バイトリーダー手当を月5000円で追加して\n\n"
            "【日数比例】出勤日数×金額\n"
            "出勤手当を1日500円で追加して\n\n"
            "【期間割増】特定期間のシフトに割増率を適用\n"
            "年末年始（12/28〜1/4）に25%の手当を追加して\n\n"
            "【時間単価】実働時間×金額\n"
            "危険手当として時給に100円追加して"
        ),
        "控除を追加したい": (
            "➖ 追加したい控除の内容を教えてください。\n\n"
            "【固定】毎月一定額が控除\n"
            "組合費を固定で月1000円の控除として追加して\n\n"
            "【日数比例】出勤日数×金額が控除\n"
            "積立金を出勤1日あたり200円の控除として追加して\n\n"
            "【定率】総支給額の◯%が控除（社会保険料的なもの向け）\n"
            "子育て支援金を総支給額の0.15%の控除として追加して\n"
            "→ %が分からない場合は金額だけでもOK\n"
            "子育て支援金を定率で今月450円引かれた分で登録して"
        ),
        "有給を取りたい": (
            "🌿 有給を取得する日付を教えてください。\n\n"
            "例：\n・7月20日に有給\n・7月20日〜22日に有給（連続取得）"
        ),
    }
    if text in _QR_PROMPTS:
        line_service.reply_text(reply_token, _QR_PROMPTS[text])
        return

    # Quick Reply 固定メッセージ：追加情報不要 → 直接ハンドラーを呼ぶ
    if text == "シフト一覧":
        _handle_list(event, user_id, {})
        return
    if text == "今月の給与確認":
        _handle_salary(event, user_id, {})
        return
    if text == "先月の給与確認":
        now = datetime.now(config.TIMEZONE)
        lm = f"{now.year - 1}/12" if now.month == 1 else f"{now.year}/{now.month - 1:02d}"
        _handle_salary(event, user_id, {"year_month": lm})
        return
    if text == "手当一覧":
        _handle_list_allowances(event, user_id)
        return
    if text == "控除一覧":
        _handle_list_custom_deductions(event, user_id)
        return
    if text == "有給残日数を確認":
        _handle_check_leave(event, user_id)
        return
    if text == "有給付与履歴":
        _handle_leave_history(event, user_id)
        return
    if text in ("仕事名一覧", "プロファイル一覧"):
        _handle_list_profiles(event, user_id)
        return
    if text == "Googleカレンダーを連携する":
        _handle_connect_calendar(event, user_id)
        return

    # 名前登録の入力待ち：自然言語から名前部分だけを抽出して登録
    if user_id in _name_input_mode:
        _name_input_mode.discard(user_id)
        name = _extract_employee_name(text)
        if name:
            user_settings.update_setting(user_id, "employee_name", name)
            line_service.reply_with_quickreply(reply_token,
                f"✅ シフト表での名前を「{name}」に登録しました。\n"
                f"次回からシフト表の写真を送ると「{name}」の行を自動で抽出します。",
                _settings_qr()
            )
        else:
            line_service.reply_text(reply_token, "名前が入力されていません。もう一度「仕事名」→「名前を設定」から登録してください。")
        return

    # 設定変更の入力待ち：値を受け取って設定を更新する
    if user_id in _setting_input_mode:
        setting_type = _setting_input_mode.pop(user_id)
        value = _parse_setting_input(setting_type, text)
        if value is None:
            label = _SETTING_LABELS.get(setting_type, setting_type)
            line_service.reply_text(reply_token, f"入力を認識できませんでした。\n{_SETTING_PROMPTS[setting_type][1]}\nのように入力してください。")
            return
        if setting_type == "notify_time":
            user_settings.update_setting(user_id, "notify_enabled", "ON")
            user_settings.update_setting(user_id, "notify_time", value)
            line_service.reply_with_quickreply(reply_token,
                f"✅ 通知時刻を {value} に設定し、通知をONにしました。",
                _settings_qr()
            )
        else:
            user_settings.update_setting(user_id, setting_type, value)
            label = _SETTING_LABELS.get(setting_type, setting_type)
            suffix = {"hourly_wage": "円", "break_minutes": "分", "leave_hours": "時間"}.get(setting_type, "")
            display = f"{value}{suffix}" if suffix else str(value)
            # カレンダー関連はカレンダー設定に戻るボタンを追加
            if setting_type == "calendar_title":
                line_service.reply_with_quickreply(reply_token,
                    f"✅ {label}を「{display}」に更新しました。",
                    [
                        {"label": "◀ カレンダー設定", "data": json.dumps({"action": "setting_calendar_menu"})},
                        {"label": "📋 設定一覧",       "data": json.dumps({"action": "setting_show"})},
                    ]
                )
            else:
                line_service.reply_with_quickreply(reply_token,
                    f"✅ {label}を「{display}」に更新しました。",
                    _settings_qr()
                )
        return

    # 有給付与日数の入力待ち：数字だけ受け取って即登録する
    if user_id in _leave_grant_input_mode:
        _leave_grant_input_mode.discard(user_id)
        import re
        m = re.search(r'\d+\.?\d*', text)
        if not m:
            line_service.reply_text(reply_token, "日数を数字で送ってください。\n例：10")
            return
        _handle_grant_leave(event, user_id, {"days": float(m.group())})
        return

    # ヘルプキーワード検索モード：次の1メッセージを検索クエリとして処理
    if user_id in _help_mode:
        _help_mode.discard(user_id)
        _handle_help_search(event, user_id, text)
        return

    try:
        parsed = shift_parser.parse_message(text)
    except Exception as e:
        logger.error(f"メッセージ解析エラー: {e}")
        line_service.reply_text(reply_token, "メッセージの解析に失敗しました。もう一度お試しください。")
        return

    intent = parsed.get("intent")
    logger.info(f"[handle_message] intent={intent}")

    if intent == "REGISTER_SHIFT":
        _handle_register(event, parsed, user_id)
    elif intent == "REGISTER_MULTIPLE_SHIFTS":
        _handle_register_multiple(event, parsed, user_id)
    elif intent == "REGISTER_SHIFTS_BATCH":
        _handle_register_batch(event, parsed, user_id)
    elif intent == "DELETE_SHIFT":
        _handle_delete(event, parsed, user_id)
    elif intent == "UPDATE_SHIFT":
        _handle_update_shift(event, parsed, user_id)
    elif intent == "LIST_SHIFTS":
        _handle_list(event, user_id, parsed)
    elif intent == "MONTHLY_SALARY":
        _handle_salary(event, user_id, parsed)
    elif intent == "UPDATE_SETTING":
        _handle_update_setting(event, user_id, parsed)
    elif intent == "CHECK_SETTING":
        line_service.reply_with_quickreply(
            reply_token,
            user_settings.format_settings(user_settings.get_settings(user_id)),
            _settings_qr()
        )
    elif intent == "GRANT_LEAVE":
        _handle_grant_leave(event, user_id, parsed)
    elif intent == "USE_LEAVE":
        _handle_use_leave(event, user_id, parsed)
    elif intent == "CHECK_LEAVE":
        _handle_check_leave(event, user_id)
    elif intent == "DELETE_LEAVE":
        _handle_delete_leave(event, user_id, parsed)
    elif intent == "MODIFY_LEAVE":
        _handle_modify_leave(event, user_id, parsed)
    elif intent == "CREATE_PROFILE":
        _handle_create_profile(event, user_id, parsed)
    elif intent == "SWITCH_PROFILE":
        _handle_switch_profile(event, user_id, parsed)
    elif intent == "UPDATE_PROFILE":
        _handle_update_profile(event, user_id, parsed)
    elif intent == "LIST_PROFILES":
        _handle_list_profiles(event, user_id)
    elif intent == "DELETE_PROFILE":
        _handle_delete_profile(event, user_id, parsed)
    elif intent == "REGISTER_DEDUCTIONS":
        _handle_register_deductions(event, user_id, parsed)
    elif intent == "DELETE_DEDUCTIONS":
        _handle_delete_deduction(event, user_id, parsed)
    elif intent == "MODIFY_DEDUCTIONS":
        _handle_modify_deduction(event, user_id, parsed)
    elif intent == "CREATE_ALLOWANCE":
        _handle_create_allowance(event, user_id, parsed)
    elif intent == "LIST_ALLOWANCES":
        _handle_list_allowances(event, user_id)
    elif intent == "DELETE_ALLOWANCE":
        _handle_delete_allowance(event, user_id, parsed)
    elif intent == "CREATE_CUSTOM_DEDUCTION":
        _handle_create_custom_deduction(event, user_id, parsed)
    elif intent == "LIST_CUSTOM_DEDUCTIONS":
        _handle_list_custom_deductions(event, user_id)
    elif intent == "DELETE_CUSTOM_DEDUCTION":
        _handle_delete_custom_deduction(event, user_id, parsed)
    elif intent == "DELETE_ALL_DATA":
        _handle_delete_all(event, user_id)
    elif intent == "CONNECT_CALENDAR":
        _handle_connect_calendar(event, user_id)
    elif intent == "DISCONNECT_CALENDAR":
        _handle_disconnect_calendar(event, user_id)
    elif intent == "HELP":
        _handle_help(event, user_id)
    else:
        line_service.reply_text(reply_token,
            "うまく解釈できませんでした。\n"
            "「ヘルプ」と送るとコマンド一覧を確認できます。\n"
            "「メニュー」と送ると操作の選択肢を表示します。"
        )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id
    _handle_image_shift(event, user_id)


def _send_shift_confirm(reply_token: str, user_id: str, shifts: list, label: str, note: str = "") -> None:
    """シフト確認メッセージを組み立てて返信する。確認後は confirm_register_multi で登録。"""
    def _norm(t: str) -> str:
        try:
            h, m = map(int, t.split(":"))
            return f"{h % 24:02d}:{m:02d}"
        except Exception:
            return t

    def _fix_year(date_str: str) -> str:
        try:
            d = datetime.strptime(date_str, "%Y/%m/%d")
            now = datetime.now(config.TIMEZONE).replace(tzinfo=None)
            if (d - now).days > 180:
                d = d.replace(year=d.year - 1)
            elif (d - now).days < -180:
                d = d.replace(year=d.year + 1)
            return d.strftime("%Y/%m/%d")
        except Exception:
            return date_str

    eff = _get_effective_settings(user_id, None)
    header = f"以下の内容で{len(shifts)}件のシフトを登録しますか？"
    if label:
        header += f"（{label}）"
    lines = [header]

    resolved = []
    unreadable_count = 0
    for s in shifts:
        if s.get("start_time") == "unreadable" or s.get("end_time") == "unreadable":
            unreadable_count += 1
            continue
        date_str   = _fix_year(s.get("date", ""))
        start_time = _norm(s.get("start_time") or eff["default_start"])
        end_time   = _norm(s.get("end_time")   or eff["default_end"])
        if not all([date_str, start_time, end_time]):
            continue
        work_min = salary_calculator.calc_work_minutes(start_time, end_time, eff["break_minutes"])
        work_str = salary_calculator.minutes_to_str(work_min)
        try:
            d = datetime.strptime(date_str, "%Y/%m/%d")
            wd = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
            lines.append(f"📅 {date_str}（{wd}）{start_time}〜{end_time} 実働{work_str}")
        except ValueError:
            lines.append(f"📅 {date_str} {start_time}〜{end_time} 実働{work_str}")
        resolved.append({
            "date": date_str, "start_time": start_time, "end_time": end_time,
            "break_minutes": eff["break_minutes"], "work_min": work_min,
            "ct": eff["calendar_title"], "ci": _resolve_color(eff["color"]), "pn": eff["profile_name"],
        })

    if not resolved:
        line_service.reply_text(reply_token, "有効なシフト情報が読み取れませんでした。")
        return
    if note:
        lines.append(f"※ {note}")
    if unreadable_count:
        lines.append(f"⚠️ {unreadable_count}件は文字が読み取れなかったため除外しました。該当日は個別に登録してください。")
    if not label:
        lines.append("\n💡 名前を登録するとあなたのシフトのみ自動抽出できます。\n「仕事名」→「名前を設定」から登録できます。")
    if eff["cutoff_day"] == 0:
        pname = eff["profile_name"] or "バイトA"
        lines.append(f"\n⚠️ 締め日が未設定のため月次給与計算ができません。\n「{pname}の締め日を25日に設定して」と送ってください。")

    _pending_multi_shifts[user_id] = {"batch": resolved}
    line_service.reply_with_quickreply(reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_register_multi"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_image_shift(event, user_id: str):
    reply_token = event.reply_token
    message_id = event.message.id

    # GPT-4o Vision処理は時間がかかるためローディングアニメーションを先に表示
    line_service.send_loading(user_id, seconds=30)

    try:
        image_bytes = line_service.get_image_content(message_id)
    except Exception as e:
        logger.error(f"画像ダウンロードエラー: {e}")
        line_service.reply_text(reply_token, "画像の取得に失敗しました。もう一度お試しください。")
        return

    gs = user_settings.get_settings(user_id)
    employee_name = gs.get("従業員名", "") or ""
    ocr_name = gs.get("OCR名", "") or ""

    try:
        result = shift_parser.parse_image_auto(image_bytes, employee_name, ocr_name)
    except Exception as e:
        logger.error(f"画像解析エラー: {e}")
        line_service.reply_text(reply_token, "画像の解析に失敗しました。もう一度お試しください。")
        return

    image_type = result.get("type", "unknown")

    if image_type == "payslip":
        _handle_image_payslip(event, user_id, result)
        return

    if image_type != "shift":
        line_service.reply_text(reply_token, "シフト表か給与明細の画像を送ってください。")
        return

    shifts = result.get("shifts", [])
    if not shifts:
        detected_names = result.get("detected_names", [])
        if employee_name and not result.get("employee_found", True) and detected_names:
            # b64・structure を保存して選択後に最初から再解析できるようにする
            _pending_name_selection[user_id] = {
                "b64":           result.get("_b64"),
                "structure":     result.get("_structure"),
                "employee_name": employee_name,
                "detected_names": detected_names,
            }
            names_text = "\n".join(f"・{n}" for n in detected_names[:10])
            qr = [{"label": n[:12], "data": json.dumps({"action": "select_shift_name", "name": n})}
                  for n in detected_names[:12]]
            qr.append({"label": "⏭️ 全員分を表示", "data": json.dumps({"action": "select_shift_name", "name": ""})})
            line_service.reply_with_quickreply(reply_token,
                f"「{employee_name}」が画像内で見つかりませんでした。\n\n"
                f"⚠️ AIが名前を読み間違えている可能性があります。\n"
                f"以下の候補の中から、あなたの名前に最も近いものをタップしてください：\n\n"
                f"{names_text}",
                qr
            )
        elif employee_name and not result.get("employee_found", True):
            line_service.reply_text(reply_token,
                f"「{employee_name}」のシフトが画像内で見つかりませんでした。\n"
                "名前の設定が合っているか確認してください。\n"
                "変更例：自分の名前を山田太郎に設定して"
            )
        else:
            line_service.reply_text(reply_token, "シフト情報を読み取れませんでした。\n画像が鮮明か確認してからもう一度お試しください。")
        return

    # GPTがOCR名を特定した場合は自動保存（次回から高速マッチ）
    if result.get("ocr_name_identified"):
        user_settings.update_setting(user_id, "ocr_name", result["ocr_name_identified"])

    _send_shift_confirm(reply_token, user_id, shifts, employee_name, result.get("note", ""))


def _handle_image_payslip(event, user_id: str, result: dict):
    """給与明細画像から読み取った控除データを確認・登録するフロー。"""
    reply_token = event.reply_token

    year_month = result.get("year_month", "")
    gross = int(result.get("gross_salary") or 0)
    if gross <= 0:
        ym_hint = f"{year_month}の" if year_month else ""
        line_service.reply_text(reply_token,
            f"📄 {ym_hint}給与明細の総支給額が読み取れませんでした。\n\n"
            "【原因として考えられること】\n"
            "・画像が暗い・ぼけている\n"
            "・明細の全体が写っていない\n"
            "・PDFのスクリーンショットで文字が小さい\n\n"
            "もう一度明細の写真を送るか、手動で入力してください。\n"
            "例：6月の明細 総支給15万 健保7755円 厚生年金13725円"
        )
        return

    field_map = {
        "健康保険": result.get("health_insurance"),
        "介護保険": result.get("nursing_insurance"),
        "厚生年金": result.get("pension"),
        "雇用保険": result.get("employment_insurance"),
        "所得税":   result.get("income_tax"),
        "住民税":   result.get("resident_tax"),
        "その他":   result.get("other"),
    }
    items = {k: int(v) for k, v in field_map.items() if v is not None}

    # 非標準控除（子育支援・財形等）を名前付きで個別管理
    raw_extra = result.get("deductions_extra") or []
    deductions_extra = [
        {"name": d["name"], "amount": int(d["amount"])}
        for d in raw_extra
        if d.get("name") and d.get("amount") and int(d["amount"]) > 0
    ]

    extra_sum = sum(d["amount"] for d in deductions_extra)
    total_deduction = sum(items.values()) + extra_sum
    net = gross - total_deduction

    # 支給手当の抽出（基本給以外）
    raw_allowances = result.get("allowances") or []
    allowances = [
        {"name": a["name"], "amount": int(a["amount"])}
        for a in raw_allowances
        if a.get("name") and a.get("amount") and int(a["amount"]) > 0
    ]

    lines = [f"以下の内容で{year_month}の明細を登録しますか？"]

    # 支給内訳
    basic = result.get("basic_salary")
    lines.append("【支給】")
    if basic:
        lines.append(f"  基本給：¥{int(basic):,}")
    for a in allowances:
        lines.append(f"  {a['name']}：¥{a['amount']:,}")
    lines.append(f"  総支給：¥{gross:,}")

    # 控除内訳
    lines.append("【控除】")
    for key in ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]:
        if items.get(key, 0) > 0:
            lines.append(f"  {key}：¥{items[key]:,}")
    for d in deductions_extra:
        lines.append(f"  {d['name']}：¥{d['amount']:,}")
    lines += [f"  合計控除：¥{total_deduction:,}", "─────────────", f"手取り：¥{net:,}"]

    note = result.get("note", "")
    if note:
        lines.append(f"※ {note}")

    # 有休使用日数
    paid_leave_days = result.get("paid_leave_days")
    if paid_leave_days and float(paid_leave_days) > 0:
        paid_leave_days = float(paid_leave_days)
        lines.append(f"\n【有休】")
        lines.append(f"  有給休暇 {paid_leave_days:g}日分を有給残高から自動で差し引きます。")
    else:
        paid_leave_days = None

    gs = user_settings.get_settings(user_id)
    active_profile = gs.get("アクティブプロファイル", "") or ""
    existing = sheets_service.get_deduction_by_month(user_id, year_month, active_profile)
    if existing:
        lines.append(
            f"\n⚠️ {year_month}の明細はすでに登録されています。\n"
            f"（総支給¥{int(existing.get('総支給額', 0) or 0):,}）\n"
            f"✏️「登録する」を押すと上書きされます。"
        )
    _pending_deductions[user_id] = {
        "year_month": year_month, "gross": gross, "items": items,
        "profile_name": active_profile, "allowances": allowances,
        "deductions_extra": deductions_extra,
        "paid_leave_days": paid_leave_days,
    }

    lines.append(
        "\n📷 内容をご確認の上、登録してください。\n"
        "※ 読み取り精度は100%ではありません。数値に誤りがある場合は手動で修正してください。\n"
        "※ アプリのスクリーンショットなど電子データのほうが、紙の写真より正確に読み取れます。"
    )

    line_service.reply_with_quickreply(reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_deductions"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _offer_next_payslip_deduction(user_id: str, prefix_msg: str = ""):
    """明細から読み取った未登録の控除項目を1件ずつ、カスタム控除として登録するか確認する。
    全件処理し終えたら、控えておいた手当の登録提案（_offer_payslip_allowances）へ引き継ぐ。"""
    pending = _pending_payslip_deductions.get(user_id)
    if not pending or not pending["items"]:
        _pending_payslip_deductions.pop(user_id, None)
        allowances = pending.get("allowances", []) if pending else []
        profile_name = pending.get("profile_name", "") if pending else ""
        work_days = pending.get("work_days", 0) if pending else 0
        if allowances:
            _offer_payslip_allowances(user_id, prefix_msg, allowances, profile_name, work_days)
        elif prefix_msg:
            line_service.push_text(user_id, f"{prefix_msg}\n次回の給与確認から手取り予測に反映されます。")
        return

    items = pending["items"]
    lines = [prefix_msg] if prefix_msg else []
    lines.append("📋 明細に登録されていない控除項目がありました。カスタム控除として登録しますか？")
    lines.append("※ 登録する項目を選ぶと、日数比例／固定／定率のどれで登録するか選べます。\n")
    for d in items:
        detail = []
        if d.get("per_day"):
            detail.append(f"{pending.get('work_days', 0)}日 → 1日¥{d['per_day']:,}")
        if d.get("rate"):
            detail.append(f"総支給額の{d['rate']:g}%")
        suffix = f"（{' / '.join(detail)}）" if detail else ""
        lines.append(f"・{d['name']}：¥{d['amount']:,}{suffix}")

    qr = []
    for i, d in enumerate(items[:11]):
        qr.append({"label": f"✅ {d['name'][:10]}", "data": json.dumps({"action": "select_payslip_deduction", "idx": i})})
    qr.append({"label": "⏭️ スキップ", "data": json.dumps({"action": "skip_payslip_deductions"})})

    line_service.push_with_quickreply(user_id, "\n".join(lines), qr)


def _offer_payslip_allowances(user_id: str, prefix_msg: str, allowances: list, profile_name: str, work_days: int):
    """明細から読み取った手当をカスタム手当として登録するか確認する。"""
    enriched = []
    for a in allowances:
        entry = dict(a)
        entry["per_day"] = round(a["amount"] / work_days) if work_days > 0 else None
        enriched.append(entry)

    _pending_payslip_allowances[user_id] = {
        "allowances": enriched, "profile_name": profile_name, "work_days": work_days
    }

    allowance_lines = []
    for a in enriched:
        if a.get("per_day"):
            allowance_lines.append(f"・{a['name']}：¥{a['amount']:,}（{work_days}日 → 1日¥{a['per_day']:,}）")
        else:
            allowance_lines.append(f"・{a['name']}：¥{a['amount']:,}")

    qr = []
    for i, a in enumerate(enriched[:11]):
        qr.append({"label": f"✅ {a['name'][:10]}", "data": json.dumps({"action": "select_payslip_allowance", "idx": i})})
    qr.append({"label": "⏭️ スキップ", "data": json.dumps({"action": "skip_allowance"})})

    lines = [prefix_msg] if prefix_msg else []
    lines.append("📋 以下の手当が読み取れました。カスタム手当として登録しますか？")
    lines.append("※ 登録する手当を選ぶと、日数比例／固定のどちらで登録するか選べます。\n")
    lines.extend(allowance_lines)

    line_service.push_with_quickreply(user_id, "\n".join(lines), qr)


def _handle_update_shift(event, parsed: dict, user_id: str):
    date_str = parsed.get("date", "")
    new_start = parsed.get("start_time")
    new_end = parsed.get("end_time")

    if not date_str or not new_start or not new_end:
        line_service.reply_text(event.reply_token,
            "変更する日付と新しい時間を指定してください。\n例：7/5のシフトを10:00〜18:00に変更して")
        return

    existing = sheets_service.get_shift_by_date(user_id, date_str)
    if not existing:
        line_service.reply_text(event.reply_token, f"📅 {date_str} のシフトが見つかりませんでした。")
        return

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        weekday = ""

    parsed_break = parsed.get("break_minutes")
    eff = _get_effective_settings(user_id, None)
    effective_break = int(parsed_break) if parsed_break is not None else eff["break_minutes"]
    work_min = salary_calculator.calc_work_minutes(new_start, new_end, effective_break)
    work_str = salary_calculator.minutes_to_str(work_min)

    old_start = existing.get("開始時刻", "")
    old_end = existing.get("終了時刻", "")

    confirm_text = (
        f"📅 {date_str}（{weekday}）のシフトを変更しますか？\n"
        f"変更前：{old_start} 〜 {old_end}\n"
        f"変更後：{new_start} 〜 {new_end}（実働{work_str}）"
    )

    _pending_update_shifts[user_id] = {
        "date": date_str, "start": new_start, "end": new_end,
        "break": effective_break, "work_min": work_min,
    }

    line_service.reply_with_quickreply(event.reply_token, confirm_text, [
        {"label": "✅ 変更する", "data": json.dumps({"action": "confirm_update_shift"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_grant_leave(event, user_id: str, parsed: dict):
    leave_type = parsed.get("type") or "年次有給"
    days = float(parsed.get("days") or 0)
    if days <= 0:
        line_service.reply_text(event.reply_token, "付与日数を指定してください。\n例：有給が10日付与されました")
        return
    granted_date = parsed.get("granted_date") or ""
    expiry_date = parsed.get("expiry_date") or ""
    note = parsed.get("note") or ""

    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]

    sheets_service.grant_leave(user_id, leave_type, days, granted_date, expiry_date, note, profile_name)

    msg_parts = [f"✅ {leave_type}を{days:g}日付与しました。"]
    if profile_name:
        msg_parts.append(f"仕事名：{profile_name}")
    if expiry_date:
        msg_parts.append(f"有効期限：{expiry_date}")
    msg_parts.append("「有給残日数を確認」で現在の残日数を確認できます。")
    line_service.reply_text(event.reply_token, "\n".join(msg_parts))


def _handle_use_leave(event, user_id: str, parsed: dict):
    date_str = parsed.get("date", "")
    leave_type = parsed.get("type") or "年次有給"
    days = float(parsed.get("days") or 1.0)

    if not date_str:
        line_service.reply_text(event.reply_token, "日付を指定してください。\n例：7/5を有給にして")
        return

    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    leave_summary = sheets_service.get_leave_summary(user_id, profile_name)
    matching = next((lt for lt in leave_summary if lt["種類"] == leave_type), None)
    remaining = matching["残日数"] if matching else 0

    # 時間の決定：メッセージ指定 → プロファイルデフォルト → 標準時間設定 の順で優先
    start_time = parsed.get("start_time") or eff["default_start"]
    end_time = parsed.get("end_time") or eff["default_end"]
    leave_hours = eff["leave_hours"]
    work_min = 0

    if start_time and end_time:
        # 明示的な時間またはプロファイルデフォルト
        break_min = eff["break_minutes"] if days == 1.0 else 0
        work_min = salary_calculator.calc_work_minutes(start_time, end_time, break_min)
        time_info = f"{start_time}〜{end_time}"
    elif leave_hours is not None:
        # プロファイルまたはグローバルの標準時間設定から計算
        total_hours = leave_hours * days
        work_min = int(total_hours * 60)
        base_start = "09:00"
        start_dt = datetime.strptime(base_start, "%H:%M")
        end_dt = start_dt + timedelta(hours=total_hours)
        start_time = base_start
        end_time = end_dt.strftime("%H:%M")
        time_info = f"{start_time}〜{end_time}（{leave_hours * days:g}時間分）"
    else:
        start_time = None
        end_time = None
        time_info = "未設定\n💡「有給標準時間を8時間に設定して」と送ると自動で計算されます"

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        weekday = ""

    day_label = "1日" if days == 1.0 else "半日（0.5日）"
    warn = f"\n⚠️ 残日数が不足しています（残{remaining:g}日）" if remaining < days else ""

    existing_shift = sheets_service.get_shift_by_date(user_id, date_str)
    overwrite_warn = ""
    if existing_shift:
        overwrite_warn = (
            f"\n⚠️ この日にはすでに {existing_shift.get('開始時刻','')}〜{existing_shift.get('終了時刻','')} "
            f"の記録があります。「取得する」を押すと上書きされます。"
        )

    confirm_text = (
        f"📅 {date_str}（{weekday}）に{leave_type}を取得しますか？\n"
        f"取得日数：{day_label}\n"
        f"時間：{time_info}"
        f"{warn}"
        f"{overwrite_warn}"
    )

    _pending_leave_usage[user_id] = {
        "date": date_str,
        "type": leave_type,
        "days": days,
        "start_time": start_time,
        "end_time": end_time,
        "work_min": work_min,
        "profile_name": profile_name,
        "calendar_title": eff["calendar_title"],
        "color": eff["color"],
    }

    line_service.reply_with_quickreply(event.reply_token, confirm_text, [
        {"label": "✅ 取得する", "data": json.dumps({"action": "confirm_use_leave"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_check_leave(event, user_id: str):
    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    leave_data = sheets_service.get_leave_summary(user_id, profile_name)
    label = f"有給残日数（{profile_name}）" if profile_name else "有給残日数"
    contents = flex_builder.build_leave_summary(leave_data, profile_name)
    line_service.reply_flex(event.reply_token, label, contents)


def _handle_leave_history(event, user_id: str):
    """有給の付与履歴を日付順で一覧表示する。"""
    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    history = sheets_service.get_leave_history(user_id, profile_name, "付与")
    profile_label = f"（{profile_name}）" if profile_name else ""

    if not history:
        line_service.reply_text(event.reply_token,
            f"📜 有給付与履歴{profile_label}\n\n"
            "まだ付与記録がありません。\n"
            "例：有給が10日付与されました")
        return

    lines = [f"📜 有給付与履歴{profile_label}"]
    for r in history:
        date_str = r.get("日付", "") or "日付不明"
        leave_type = r.get("種類", "")
        days = r.get("日数", "")
        try:
            days_disp = f"{float(days):g}日"
        except (ValueError, TypeError):
            days_disp = f"{days}日"
        entry = f"\n■ {date_str}　{leave_type} {days_disp}"
        expiry = r.get("有効期限", "")
        if expiry:
            entry += f"\n   有効期限：{expiry}"
        note = r.get("備考", "")
        if note:
            entry += f"\n   備考：{note}"
        lines.append(entry)

    lines.append("\n現在の残日数は「有給残日数を確認」でご確認いただけます。")
    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_delete_leave(event, user_id: str, parsed: dict):
    date_str = parsed.get("date", "")
    if not date_str:
        line_service.reply_text(event.reply_token, "削除する有給の日付を指定してください。\n例：7/5の有給を削除して")
        return

    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    shift = sheets_service.get_shift_by_date(user_id, date_str)
    leave_summary = sheets_service.get_leave_summary(user_id, profile_name)

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        weekday = ""

    if not shift:
        line_service.reply_text(event.reply_token, f"{date_str}の有給記録が見つかりませんでした。")
        return

    work_str = salary_calculator.minutes_to_str(int(shift.get("実働時間(分)", 0) or 0))
    msg = (
        f"📅 {date_str}（{weekday}）の有給を削除しますか？\n"
        f"⏰ {shift.get('開始時刻', '')}〜{shift.get('終了時刻', '')}（{work_str}）\n"
        f"⚠️ 削除すると残日数が1日分戻ります。"
    )
    _pending_delete_leave[user_id] = {
        "date": date_str,
        "profile_name": profile_name,
        "event_id": shift.get("Calendar EventID", ""),
    }
    line_service.reply_with_quickreply(event.reply_token, msg, [
        {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_leave"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_modify_leave(event, user_id: str, parsed: dict):
    date_str = parsed.get("date", "")
    new_days = parsed.get("days")
    new_type = parsed.get("type")
    if not date_str or (new_days is None and not new_type):
        line_service.reply_text(event.reply_token,
            "変更内容を指定してください。\n"
            "例：7/5の有給を半日に変更して\n"
            "　　7/5の有給を振替休日に変更して")
        return

    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    shift = sheets_service.get_shift_by_date(user_id, date_str)
    if not shift:
        line_service.reply_text(event.reply_token, f"{date_str}の有給記録が見つかりませんでした。")
        return

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        weekday = ""

    changes = []
    if new_days is not None:
        changes.append(f"取得日数：{'1日' if float(new_days) == 1.0 else '半日（0.5日）'}")
    if new_type:
        changes.append(f"種類：{new_type}")

    msg = (
        f"📅 {date_str}（{weekday}）の有給を変更しますか？\n"
        + "\n".join(changes)
        + "\n※ 内部的に削除→再登録されます。"
    )
    _pending_modify_leave[user_id] = {
        "date": date_str,
        "new_days": float(new_days) if new_days is not None else None,
        "new_type": new_type,
        "profile_name": profile_name,
        "event_id": shift.get("Calendar EventID", ""),
        "old_start": shift.get("開始時刻", ""),
        "old_end": shift.get("終了時刻", ""),
        "eff": eff,
    }
    line_service.reply_with_quickreply(event.reply_token, msg, [
        {"label": "✅ 変更する", "data": json.dumps({"action": "confirm_modify_leave"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _get_premium_settings(user_id: str) -> dict:
    """深夜・早朝手当の設定をユーザー設定から取得する。"""
    gs = user_settings.get_settings(user_id)
    return {
        "night_rate": float(gs.get("深夜割増率", config.DEFAULT_NIGHT_RATE) or config.DEFAULT_NIGHT_RATE) / 100,
        "early_rate": float(gs.get("早朝割増率", config.DEFAULT_EARLY_RATE) or config.DEFAULT_EARLY_RATE) / 100,
        "early_end":  gs.get("早朝終了時刻", config.DEFAULT_EARLY_END) or config.DEFAULT_EARLY_END,
    }


def _resolve_color(color_name: str) -> str:
    """色名をGoogleカレンダーのcolorIdに変換する。"""
    return config.CALENDAR_COLOR_MAP.get((color_name or "").strip(), "")


def _get_effective_settings(user_id: str, label: str | None) -> dict:
    """プロファイル名またはアクティブプロファイルに基づいて有効な設定を返す。
    label がプロファイル名と一致すればそのプロファイルを、なければアクティブプロファイルを、
    それもなければグローバル設定を使用する。"""
    gs = user_settings.get_settings(user_id)
    g_title = gs.get("カレンダータイトル", config.DEFAULT_CALENDAR_TITLE) or config.DEFAULT_CALENDAR_TITLE
    g_break = int(gs.get("休憩時間(分)", config.DEFAULT_BREAK_MINUTES))
    g_color = gs.get("カレンダーカラー", "") or ""
    g_wage  = int(gs.get("時給(円)", config.DEFAULT_HOURLY_WAGE))
    g_leave_hours_raw = gs.get("有給標準時間(時間)", "") or ""
    try:
        g_leave_hours = float(g_leave_hours_raw) if g_leave_hours_raw else None
    except (ValueError, TypeError):
        g_leave_hours = None

    profile = None
    if label:
        profile = sheets_service.get_profile(user_id, label)
    if profile is None:
        active = gs.get("アクティブプロファイル", "") or ""
        if active:
            profile = sheets_service.get_profile(user_id, active)
        if profile is None:
            # アクティブプロファイルが削除済み・リネーム済みなどで参照切れの場合、
            # 登録済みプロファイルが1件だけならそれを暫定的に使用する（設定の消失を防ぐ）
            all_profiles = sheets_service.get_profiles(user_id)
            if len(all_profiles) == 1:
                profile = all_profiles[0]

    if profile:
        p_lh_raw = str(profile.get("有給標準時間(時間)", "") or "")
        try:
            p_leave_hours = float(p_lh_raw) if p_lh_raw else g_leave_hours
        except (ValueError, TypeError):
            p_leave_hours = g_leave_hours
        p_break_raw = profile.get("休憩時間(分)", "")
        p_wage_raw = profile.get("時給(円)", "")
        return {
            "calendar_title": profile.get("カレンダータイトル") or g_title,
            "default_start":  profile.get("デフォルト開始時刻") or "",
            "default_end":    profile.get("デフォルト終了時刻") or "",
            "break_minutes":  int(p_break_raw) if p_break_raw != "" else g_break,
            "color":          profile.get("カレンダーカラー") or g_color,
            "hourly_wage":    int(p_wage_raw) if p_wage_raw != "" else g_wage,
            "profile_name":   profile.get("プロファイル名", ""),
            "cutoff_day":     int(profile.get("締め日") or 0),
            "payday":         profile.get("給料日") or "",
            "leave_hours":    p_leave_hours,
        }
    return {
        "calendar_title": label or g_title,
        "default_start":  "",
        "default_end":    "",
        "break_minutes":  g_break,
        "color":          g_color,
        "hourly_wage":    g_wage,
        "profile_name":   "",
        "cutoff_day":     0,
        "payday":         "",
        "leave_hours":    g_leave_hours,
    }


def _estimate_current_gross(user_id: str, profile_name: str | None = None) -> int | None:
    """現在の給与サイクルの総支給額（概算）を返す。締め日未設定などで算出できない場合はNoneを返す。"""
    eff = _get_effective_settings(user_id, profile_name)
    if eff["cutoff_day"] == 0:
        return None

    ps = _get_premium_settings(user_id)
    now = datetime.now(config.TIMEZONE)
    start_str, end_str, _ = salary_calculator.get_pay_period(eff["cutoff_day"], now)
    shifts = sheets_service.get_shifts_in_period(user_id, start_str, end_str, eff["profile_name"])

    result = salary_calculator.aggregate_monthly(
        shifts, eff["hourly_wage"], ps["night_rate"], ps["early_rate"], ps["early_end"]
    )
    allowances = sheets_service.get_allowances(user_id, eff["profile_name"])
    result = salary_calculator.apply_allowances(result, shifts, allowances, eff["hourly_wage"])
    return result["salary"] or None


def _send_oauth_prompt(reply_token: str, user_id: str) -> None:
    """Googleカレンダー連携URLを案内する。"""
    start_url = f"{config.APP_BASE_URL}/oauth/start?user_id={user_id}"
    line_service.reply_text(reply_token,
        f"以下のURLをタップしてGoogleカレンダーと連携できます。\n"
        f"（URLの有効期限は10分です）\n\n"
        f"{start_url}"
    )


def _handle_delete_all(event, user_id: str) -> None:
    """全データ削除の第1段階確認。"""
    line_service.reply_with_quickreply(event.reply_token,
        "⚠️ 全データ削除の確認（1/2）\n\n"
        "以下のデータがすべて削除されます：\n"
        "・シフトデータ（全期間）\n"
        "・給与明細・控除データ\n"
        "・有給管理データ\n"
        "・仕事名設定\n"
        "・カスタム手当\n"
        "・カスタム控除\n"
        "・Googleカレンダー連携トークン\n\n"
        "この操作は取り消せません。\n"
        "本当に続けますか？", [
            {"label": "続ける", "data": json.dumps({"action": "delete_all_stage1"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ])


def _handle_help(event, user_id: str) -> None:
    """ヘルプキーワード検索モードを開始する。"""
    _help_mode.add(user_id)
    line_service.reply_with_quickreply(event.reply_token,
        "📖 ヘルプ\n\n"
        "調べたいキーワードを送るか、\n"
        "カテゴリ一覧からカテゴリを選んでください。\n\n"
        "（例：シフト、有給、給与、仕事名、通知...）",
        [
            {"label": "📖 カテゴリ一覧", "data": json.dumps({"action": "help_show_all"})},
            {"label": "⚙️ 現在の設定",  "data": json.dumps({"action": "help_category", "cat": "settings"})},
        ]
    )


def _handle_help_search(event, user_id: str, query: str) -> None:
    """ヘルプキーワード検索モード中のメッセージを処理する。"""
    reply_token = event.reply_token

    # 「一覧」系キーワード → カテゴリ一覧を表示
    if any(kw in query for kw in ("一覧", "全部", "すべて", "全て", "リスト", "全機能")):
        cats = help_content.get_category_list()
        qr = [{"label": c["label"], "data": json.dumps({"action": "help_category", "cat": c["cat"]})} for c in cats]
        qr.append({"label": "🔍 キーワード検索", "data": json.dumps({"action": "help_search_again"})})
        line_service.reply_with_quickreply(reply_token, help_content.get_all_text(), qr)
        return

    matches = help_content.search(query)

    if not matches:
        _help_mode.add(user_id)
        cats = help_content.get_category_list()
        qr = [{"label": c["label"], "data": json.dumps({"action": "help_category", "cat": c["cat"]})} for c in cats]
        qr.append({"label": "🔍 再検索", "data": json.dumps({"action": "help_search_again"})})
        line_service.reply_with_quickreply(reply_token,
            f"「{query}」に関する機能が見つかりませんでした。\n"
            "カテゴリから探してみてください。",
            qr
        )
        return

    if len(matches) == 1:
        item = matches[0]
        detail = _build_settings_help(user_id) if item.get("detail") == "__LIVE_SETTINGS__" else item["detail"]
        back_cat = item.get("category", "")
        line_service.reply_with_quickreply(reply_token, detail, [
            {"label": "◀ カテゴリに戻る", "data": json.dumps({"action": "help_category", "cat": back_cat})},
            {"label": "🔍 再検索",         "data": json.dumps({"action": "help_search_again"})},
            {"label": "📖 カテゴリ一覧",   "data": json.dumps({"action": "help_show_all"})},
        ])
        return

    # 複数件 → 候補をQuick Replyで表示（最大11件+再検索）
    qr_items = [
        {"label": m["title"][:20], "data": json.dumps({"action": "help_item", "id": m["id"]})}
        for m in matches[:11]
    ]
    qr_items.append({"label": "🔍 再検索", "data": json.dumps({"action": "help_search_again"})})

    line_service.reply_with_quickreply(reply_token,
        f"「{query}」の検索結果：{len(matches)}件\n詳細を見たい項目を選んでください。",
        qr_items
    )


def _build_settings_help(user_id: str) -> str:
    """現在の設定状況を ✅/⚠️ 付きで返す。"""
    gs = user_settings.get_settings(user_id)
    has_cal = sheets_service.has_google_token(user_id)
    sep = "━" * 16

    def row(label: str, val: str, required: bool = False) -> str:
        if val:
            return f"✅ {label}：{val}"
        return f"{'⚠️' if required else '・'} {label}：未設定"

    hourly_wage   = gs.get("時給(円)", "")
    break_min     = gs.get("休憩時間(分)", "")
    notify        = gs.get("通知ON/OFF", "")
    notify_time   = gs.get("通知時刻", "")
    display_name  = gs.get("表示名", "")
    employee_name = gs.get("従業員名", "")
    active_prof   = gs.get("アクティブプロファイル", "")
    leave_hours   = gs.get("有給標準時間(時間)", "")
    social_ins    = gs.get("社会保険加入", "なし") or "なし"

    if notify == "ON":
        notify_row = f"✅ 通知：ON（{notify_time}）"
    elif notify == "OFF":
        notify_row = "・ 通知：OFF"
    else:
        notify_row = "⚠️ 通知：未設定"

    cal_row = f"✅ Googleカレンダー：連携済み" if has_cal else "・ Googleカレンダー：未連携"

    cal_title = gs.get("カレンダータイトル", "") or ""

    lines = [
        "⚙️ 現在の設定", sep,
        row("表示名",             display_name),
        row("時給",               f"¥{int(hourly_wage):,}" if hourly_wage else "", required=True),
        row("休憩時間",           f"{break_min}分" if break_min else ""),
        notify_row,
        row("シフト表での名前",   employee_name),
        row("カレンダー予定名",   cal_title or "シフト（デフォルト）"),
        row("有給標準時間",       f"{leave_hours}時間" if leave_hours else ""),
        row("選択中の仕事名",       active_prof or "なし"),
        f"・ 社会保険：{social_ins}",
        cal_row,
        sep,
        "⚠️ = 設定をおすすめします",
        "設定変更例：時給を1200円に設定して",
    ]
    return "\n".join(lines)


def _handle_connect_calendar(event, user_id: str) -> None:
    """Googleカレンダー連携を開始する（メッセージイベント用）。"""
    _handle_connect_calendar_postback(event.reply_token, user_id)


def _handle_connect_calendar_postback(reply_token: str, user_id: str) -> None:
    """Googleカレンダー連携を開始する（ポストバック・共通処理）。"""
    if sheets_service.has_google_token(user_id):
        line_service.reply_with_quickreply(reply_token,
            "✅ すでにGoogleカレンダーと連携済みです。",
            [
                {"label": "🔓 連携を解除", "data": json.dumps({"action": "disconnect_calendar"})},
                {"label": "◀ カレンダー設定", "data": json.dumps({"action": "setting_calendar_menu"})},
            ]
        )
        return
    _send_oauth_prompt(reply_token, user_id)


def _handle_disconnect_calendar(event, user_id: str) -> None:
    """Googleカレンダーの連携を解除する（メッセージイベント用）。"""
    _handle_disconnect_calendar_postback(event.reply_token, user_id)


def _handle_disconnect_calendar_postback(reply_token: str, user_id: str) -> None:
    """Googleカレンダーの連携を解除する（ポストバック・共通処理）。"""
    if not sheets_service.has_google_token(user_id):
        line_service.reply_text(reply_token,
            "Googleカレンダーはまだ連携されていません。")
        return
    line_service.reply_with_quickreply(reply_token,
        "🔗 Googleカレンダーとの連携を解除しますか？\n"
        "解除後もシフトデータはスプレッドシートに保存され続けます。\n"
        "カレンダーへの登録・削除のみできなくなります。", [
            {"label": "🔓 解除する", "data": json.dumps({"action": "confirm_disconnect_calendar"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ])


def _handle_register(event, parsed: dict, user_id: str):
    label = parsed.get("title")
    eff = _get_effective_settings(user_id, label)

    date_str = parsed.get("date", "")
    start_time = parsed.get("start_time") or eff["default_start"]
    end_time   = parsed.get("end_time")   or eff["default_end"]

    if not date_str:
        line_service.reply_text(event.reply_token, "日付の解析に失敗しました。\n例：7/5 9:00〜17:00")
        return
    if not start_time or not end_time:
        line_service.reply_text(event.reply_token, "時刻を指定してください。\n例：7/5 9:00〜17:00")
        return

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        line_service.reply_text(event.reply_token, "日付の形式が正しくありません。")
        return

    parsed_break = parsed.get("break_minutes")
    effective_break = int(parsed_break) if parsed_break is not None else eff["break_minutes"]
    msg_color = parsed.get("color")
    effective_color_name = msg_color or eff["color"]
    color_id = _resolve_color(effective_color_name)
    calendar_title = eff["calendar_title"]
    display_color = effective_color_name or "デフォルト"

    ps = _get_premium_settings(user_id)
    summary = salary_calculator.calc_shift_summary(
        start_time, end_time, effective_break, eff["hourly_wage"],
        ps["night_rate"], ps["early_rate"], ps["early_end"]
    )
    work_str = salary_calculator.minutes_to_str(summary["work_min"])

    existing_shift = sheets_service.get_shift_by_date(user_id, date_str)
    profile_label = f"（{eff['profile_name']}）" if eff["profile_name"] else ""
    overwrite_warn = ""
    if existing_shift:
        overwrite_warn = (
            f"\n⚠️ この日にはすでに {existing_shift.get('開始時刻','')}〜{existing_shift.get('終了時刻','')} "
            f"のシフトが登録されています。\n✏️「登録する」を押すと上書きされます。"
        )
    lines = [
        f"以下の内容で登録しますか？{profile_label}",
        f"📅 {date_str}（{weekday}）",
        f"⏰ {start_time} 〜 {end_time}",
        f"🕐 実働 {work_str}（休憩{effective_break}分）",
        f"📌 {calendar_title}  🎨 {display_color}",
    ]
    if summary["night_min"] > 0:
        lines.append(f"🌙 深夜 {salary_calculator.minutes_to_str(summary['night_min'])} → 手当+¥{summary['night_premium']:,}")
    if summary["early_min"] > 0 and ps["early_rate"] > 0:
        lines.append(f"🌅 早朝 {salary_calculator.minutes_to_str(summary['early_min'])} → 手当+¥{summary['early_premium']:,}")
    # 期間割増手当のプレビュー
    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        allowances = sheets_service.get_allowances(user_id, eff["profile_name"])
        for a in allowances:
            if a.get("タイプ") == "期間割増" and str(a.get("有効", "yes")).lower() not in ("no", "false", "0"):
                sm, sd = int(a.get("期間開始月", 0) or 0), int(a.get("期間開始日", 0) or 0)
                em, ed = int(a.get("期間終了月", 0) or 0), int(a.get("期間終了日", 0) or 0)
                if salary_calculator._date_in_period(d.month, d.day, sm, sd, em, ed):
                    rate = float(a.get("割増率(%)", 0) or 0)
                    bonus = round(summary["work_min"] / 60 * eff["hourly_wage"] * rate / 100)
                    lines.append(f"🎁 {a['手当名']}（{int(rate)}%割増）→ +¥{bonus:,}")
                    summary["total"] += bonus
    except Exception:
        pass
    if summary["night_min"] > 0 or (summary["early_min"] > 0 and ps["early_rate"] > 0):
        lines.append(f"💴 予想給与：¥{summary['total']:,}")
    if overwrite_warn:
        lines.append(overwrite_warn)
    confirm_text = "\n".join(lines)
    data = json.dumps(
        {"action": "confirm_register", "date": date_str, "start": start_time, "end": end_time,
         "break": effective_break, "ct": calendar_title, "ci": color_id, "pn": eff["profile_name"]},
        ensure_ascii=False,
    )
    line_service.reply_with_quickreply(event.reply_token, confirm_text, [
        {"label": "✅ 登録する", "data": data},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_register_multiple(event, parsed: dict, user_id: str):
    label = parsed.get("title")
    eff = _get_effective_settings(user_id, label)

    dates = parsed.get("dates", [])
    start_time = parsed.get("start_time") or eff["default_start"]
    end_time   = parsed.get("end_time")   or eff["default_end"]

    if not dates or not start_time or not end_time:
        line_service.reply_text(event.reply_token, "日付または時刻の解析に失敗しました。\n例：7/5 7/6 7/7 14:00〜22:00")
        return

    parsed_break = parsed.get("break_minutes")
    effective_break = int(parsed_break) if parsed_break is not None else eff["break_minutes"]
    msg_color = parsed.get("color")
    effective_color_name = msg_color or eff["color"]
    color_id = _resolve_color(effective_color_name)
    calendar_title = eff["calendar_title"]
    display_color = effective_color_name or "デフォルト"

    work_min = salary_calculator.calc_work_minutes(start_time, end_time, effective_break)
    work_str = salary_calculator.minutes_to_str(work_min)

    profile_label = f"（{eff['profile_name']}）" if eff["profile_name"] else ""
    duplicate_dates = [d for d in dates if sheets_service.get_shift_by_date(user_id, d)]
    lines = [f"以下の内容で{len(dates)}日分まとめて登録しますか？{profile_label}"]
    for date_str in dates:
        try:
            d = datetime.strptime(date_str, "%Y/%m/%d")
            weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
            dup_mark = " ⚠️上書き" if date_str in duplicate_dates else ""
            lines.append(f"📅 {date_str}（{weekday}）{dup_mark}")
        except ValueError:
            lines.append(f"📅 {date_str}")
    if duplicate_dates:
        lines.append(f"⚠️ ⚠️マークの{len(duplicate_dates)}日は既存のシフトを上書きします。")
    lines.append(f"⏰ {start_time} 〜 {end_time}  実働 {work_str}（休憩{effective_break}分）")
    lines.append(f"📌 {calendar_title}  🎨 {display_color}")

    _pending_multi_shifts[user_id] = {
        "dates": dates,
        "start_time": start_time,
        "end_time": end_time,
        "break_minutes": effective_break,
        "ct": calendar_title,
        "ci": color_id,
        "pn": eff["profile_name"],
    }

    line_service.reply_with_quickreply(event.reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_register_multi"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_register_batch(event, parsed: dict, user_id: str):
    shifts = parsed.get("shifts", [])
    if not shifts:
        line_service.reply_text(event.reply_token, "シフト情報の解析に失敗しました。もう一度お試しください。")
        return

    lines = [f"以下の内容で{len(shifts)}日分まとめて登録しますか？（⚠️マークは上書き）"]
    resolved = []
    for s in shifts:
        label = s.get("title")
        eff = _get_effective_settings(user_id, label)

        date_str  = s.get("date", "")
        start_time = s.get("start_time") or eff["default_start"]
        end_time   = s.get("end_time")   or eff["default_end"]
        parsed_break = s.get("break_minutes")
        effective_break = int(parsed_break) if parsed_break is not None else eff["break_minutes"]
        msg_color = s.get("color")
        effective_color_name = msg_color or eff["color"]
        color_id = _resolve_color(effective_color_name)
        calendar_title = eff["calendar_title"]

        if not all([date_str, start_time, end_time]):
            continue

        work_min = salary_calculator.calc_work_minutes(start_time, end_time, effective_break)
        work_str = salary_calculator.minutes_to_str(work_min)

        try:
            d = datetime.strptime(date_str, "%Y/%m/%d")
            weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
        except ValueError:
            weekday = ""

        profile_tag = f"（{eff['profile_name']}）" if eff["profile_name"] else ""
        display_color = effective_color_name or ""
        extras = f" 📌{calendar_title}" + (f" 🎨{display_color}" if display_color else "")
        dup_mark = " ⚠️上書き" if sheets_service.get_shift_by_date(user_id, date_str) else ""
        lines.append(f"📅 {date_str}（{weekday}）{start_time}〜{end_time} 実働{work_str}（休憩{effective_break}分）{extras}{profile_tag}{dup_mark}")
        resolved.append({
            "date": date_str, "start_time": start_time, "end_time": end_time,
            "break_minutes": effective_break, "work_min": work_min,
            "ct": calendar_title, "ci": color_id, "pn": eff["profile_name"],
        })

    if not resolved:
        line_service.reply_text(event.reply_token, "有効なシフト情報が見つかりませんでした。")
        return

    _pending_multi_shifts[user_id] = {"batch": resolved}

    line_service.reply_with_quickreply(event.reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_register_multi"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_delete(event, parsed: dict, user_id: str):
    date_str = parsed.get("date", "")
    if not date_str:
        line_service.reply_text(event.reply_token, "削除する日付を確認できませんでした。")
        return

    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
        weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    except ValueError:
        weekday = ""

    confirm_text = f"📅 {date_str}（{weekday}）のシフトを削除しますか？"
    line_service.reply_with_quickreply(event.reply_token, confirm_text, [
        {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete", "date": date_str})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_list(event, user_id: str, parsed: dict = None):
    now = datetime.now(config.TIMEZONE)
    profile_name = parsed.get("profile_name") if parsed else None
    eff = _get_effective_settings(user_id, profile_name)

    year_month = parsed.get("year_month") if parsed else None
    if year_month:
        try:
            target = datetime.strptime(year_month, "%Y/%m")
            start_str, end_str, period_label = salary_calculator.get_pay_period_for_month(
                eff["cutoff_day"], target.year, target.month
            )
        except ValueError:
            start_str, end_str, period_label = salary_calculator.get_pay_period(eff["cutoff_day"], now)
    else:
        start_str, end_str, period_label = salary_calculator.get_pay_period(eff["cutoff_day"], now)

    shifts = sheets_service.get_shifts_in_period(user_id, start_str, end_str, eff["profile_name"])
    profile_label = f"（{eff['profile_name']}）" if eff["profile_name"] else ""

    if not shifts:
        line_service.reply_text(event.reply_token, f"{period_label}のシフトはまだ登録されていません。{profile_label}")
        return

    contents = flex_builder.build_shift_list(shifts, period_label + profile_label)
    line_service.reply_flex(event.reply_token, f"{period_label}のシフト一覧{profile_label}", contents)


def _handle_salary(event, user_id: str, parsed: dict = None):
    now = datetime.now(config.TIMEZONE)
    profile_name = parsed.get("profile_name") if parsed else None
    eff = _get_effective_settings(user_id, profile_name)

    # 締め日未設定は給与計算不可
    if eff["cutoff_day"] == 0:
        profile_hint = f"「{eff['profile_name']}」の" if eff["profile_name"] else ""
        line_service.reply_text(event.reply_token,
            f"⚠️ 給与計算には締め日の設定が必要です。\n\n"
            f"{profile_hint}締め日を設定してください。\n"
            f"例：「{eff['profile_name'] or 'バイトA'}の締め日を25日に設定して」")
        return

    hourly_wage = eff["hourly_wage"]

    gs = user_settings.get_settings(user_id)
    social_insurance = (gs.get("社会保険加入", "なし") or "なし") == "あり"
    ps = _get_premium_settings(user_id)

    year_month = parsed.get("year_month") if parsed else None
    if year_month:
        try:
            target = datetime.strptime(year_month, "%Y/%m")
            start_str, end_str, period_label = salary_calculator.get_pay_period_for_month(
                eff["cutoff_day"], target.year, target.month
            )
        except ValueError:
            start_str, end_str, period_label = salary_calculator.get_pay_period(eff["cutoff_day"], now)
    else:
        start_str, end_str, period_label = salary_calculator.get_pay_period(eff["cutoff_day"], now)

    shifts = sheets_service.get_shifts_in_period(user_id, start_str, end_str, eff["profile_name"])
    result = salary_calculator.aggregate_monthly(
        shifts, hourly_wage, ps["night_rate"], ps["early_rate"], ps["early_end"]
    )

    allowances = sheets_service.get_allowances(user_id, eff["profile_name"])
    result = salary_calculator.apply_allowances(result, shifts, allowances, hourly_wage)
    gross = result["salary"]

    deduction_records = sheets_service.get_deductions(user_id, eff["profile_name"])
    pred = salary_calculator.predict_deductions(gross, deduction_records)
    if pred is None:
        pred = salary_calculator.estimate_deductions_default(gross, social_insurance)

    custom_deductions = sheets_service.get_custom_deductions(user_id, eff["profile_name"])
    if custom_deductions:
        pred = salary_calculator.apply_custom_deductions(pred, result["work_days"], custom_deductions, gross)

    payday_note = eff.get("payday", "") or ""
    profile_label = f"（{eff['profile_name']}）" if eff["profile_name"] else ""
    display_label = period_label + profile_label
    contents = flex_builder.build_salary_summary(result, display_label, hourly_wage, pred, ps, payday_note)
    line_service.reply_flex(event.reply_token, f"{display_label}の給与予測", contents)


# ── プロファイル管理ハンドラー ────────────────────────

def _handle_create_profile(event, user_id: str, parsed: dict):
    name = parsed.get("name", "").strip()
    if not name:
        line_service.reply_text(event.reply_token, "仕事名を指定してください。\n例：バイトA を追加して 9:00〜17:00 時給1200円")
        return

    fields = {"プロファイル名": name}
    if parsed.get("calendar_title"):
        fields["カレンダータイトル"] = parsed["calendar_title"]
    else:
        fields["カレンダータイトル"] = name
    if parsed.get("start_time"):
        fields["デフォルト開始時刻"] = parsed["start_time"]
    if parsed.get("end_time"):
        fields["デフォルト終了時刻"] = parsed["end_time"]
    if parsed.get("break_minutes") is not None:
        fields["休憩時間(分)"] = int(parsed["break_minutes"])
    if parsed.get("color"):
        fields["カレンダーカラー"] = parsed["color"]
    if parsed.get("hourly_wage"):
        fields["時給(円)"] = int(parsed["hourly_wage"])
    if parsed.get("leave_hours") is not None:
        fields["有給標準時間(時間)"] = float(parsed["leave_hours"])

    sheets_service.upsert_profile(user_id, name, fields)

    parts = [f"✅ 仕事名「{name}」を登録しました。"]
    if fields.get("デフォルト開始時刻") and fields.get("デフォルト終了時刻"):
        parts.append(f"⏰ {fields['デフォルト開始時刻']}〜{fields['デフォルト終了時刻']}")
    if "休憩時間(分)" in fields:
        parts.append(f"🕐 休憩{fields['休憩時間(分)']}分")
    if "カレンダーカラー" in fields:
        parts.append(f"🎨 {fields['カレンダーカラー']}")
    if "時給(円)" in fields:
        parts.append(f"💴 時給{fields['時給(円)']}円")
    if "有給標準時間(時間)" in fields:
        parts.append(f"⏱️ 有給標準時間{fields['有給標準時間(時間)']:g}時間")
    if "締め日" not in fields:
        parts.append(f"\n⚠️ 締め日が未設定です。月次給与計算を使うには締め日を設定してください。\n例：「{name}の締め日を25日に設定して」")
    line_service.reply_text(event.reply_token, "\n".join(parts))


def _handle_switch_profile(event, user_id: str, parsed: dict):
    name = parsed.get("name", "").strip()
    if not name:
        line_service.reply_text(event.reply_token, "切り替える仕事名を指定してください。")
        return
    profile = sheets_service.get_profile(user_id, name)
    if not profile:
        line_service.reply_text(event.reply_token, f"仕事名「{name}」が見つかりません。\nまず登録してください。")
        return
    user_settings.update_setting(user_id, "active_profile", name)
    line_service.reply_text(event.reply_token,
        f"✅ 「{name}」に切り替えました。\n以降のシフト登録はこの仕事名の設定が使われます。")


def _handle_update_profile(event, user_id: str, parsed: dict):
    name = parsed.get("name", "").strip()
    field_key = parsed.get("field", "")
    value = parsed.get("value")

    field_map = {
        "calendar_title": "カレンダータイトル",
        "start_time":     "デフォルト開始時刻",
        "end_time":       "デフォルト終了時刻",
        "break_minutes":  "休憩時間(分)",
        "color":          "カレンダーカラー",
        "hourly_wage":    "時給(円)",
        "cutoff_day":     "締め日",
        "payday":         "給料日",
        "leave_hours":    "有給標準時間(時間)",
    }
    sheet_field = field_map.get(field_key)
    if not name or not sheet_field or value is None:
        line_service.reply_text(event.reply_token, "仕事名・項目・値を指定してください。")
        return

    sheets_service.upsert_profile(user_id, name, {sheet_field: value})
    line_service.reply_text(event.reply_token, f"✅ 「{name}」の{sheet_field}を「{value}」に更新しました。")


def _handle_list_profiles(event, user_id: str):
    profiles = sheets_service.get_profiles(user_id)
    gs = user_settings.get_settings(user_id)
    active = gs.get("アクティブプロファイル", "") or ""

    if not profiles:
        line_service.reply_text(event.reply_token,
            "仕事名がまだ登録されていません。\n例：「バイトA を追加して 9:00〜17:00 時給1200円 休憩60分」と送ってください。")
        return

    no_cutoff = []
    lines = ["📋 仕事名一覧"]
    for p in profiles:
        n = p.get("プロファイル名", "")
        marker = " ✅（選択中）" if n == active else ""
        cutoff = int(p.get("締め日") or 0)
        parts = [f"■ {n}{marker}"]
        if p.get("デフォルト開始時刻") and p.get("デフォルト終了時刻"):
            parts.append(f"  ⏰ {p['デフォルト開始時刻']}〜{p['デフォルト終了時刻']}")
        if p.get("休憩時間(分)"):
            parts.append(f"  🕐 休憩{p['休憩時間(分)']}分")
        if p.get("カレンダーカラー"):
            parts.append(f"  🎨 {p['カレンダーカラー']}")
        if p.get("時給(円)"):
            parts.append(f"  💴 時給{p['時給(円)']}円")
        if cutoff:
            parts.append(f"  📅 締め日：毎月{cutoff}日")
        else:
            parts.append(f"  📅 締め日：未設定 ⚠️（給与計算に必要）")
            no_cutoff.append(n)
        if p.get("有給標準時間(時間)"):
            parts.append(f"  ⏱️ 有給標準時間{p['有給標準時間(時間)']}時間")
        lines.append("\n".join(parts))

    if no_cutoff:
        names = "・".join(no_cutoff)
        lines.append(
            f"\n💡 締め日を設定すると月次給与計算・レポートが使えます。\n"
            f"例：「{no_cutoff[0]}の締め日を25日に設定して」"
        )

    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_delete_profile(event, user_id: str, parsed: dict):
    name = parsed.get("name", "").strip()
    if not name:
        line_service.reply_text(event.reply_token, "削除する仕事名を指定してください。")
        return
    if not sheets_service.get_profile(user_id, name):
        line_service.reply_text(event.reply_token, f"仕事名「{name}」が見つかりませんでした。")
        return
    line_service.reply_with_quickreply(event.reply_token,
        f"⚠️ 仕事名「{name}」削除の確認（1/2）\n\n"
        f"以下のデータがすべて削除されます：\n"
        f"・仕事名の設定（時給・休憩時間・締め日など）\n"
        f"・この仕事名で登録したシフトデータ\n"
        f"・この仕事名の給与明細・控除データ\n"
        f"・この仕事名専用のカスタム手当\n"
        f"・この仕事名専用のカスタム控除\n\n"
        f"※ Googleカレンダー上の予定はそのまま残ります（削除されません）。\n"
        f"この操作は取り消せません。\n"
        f"本当に続けますか？",
        [
            {"label": "続ける", "data": json.dumps({"action": "delete_profile_stage1", "name": name})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


# ── カスタム手当ハンドラー ───────────────────────────

_TYPE_LABEL = {"月額固定": "月額固定", "日数比例": "日数比例", "期間割増": "期間割増", "時間単価": "時間単価"}

def _handle_create_allowance(event, user_id: str, parsed: dict):
    name = (parsed.get("name") or "").strip()
    atype = parsed.get("type", "")
    if not name or atype not in _TYPE_LABEL:
        line_service.reply_text(event.reply_token,
            "手当名とタイプを指定してください。\n"
            "例：バイトリーダー手当を月5000円で追加して\n"
            "例：年末年始（12/28〜1/4）に25%の手当を追加して\n"
            "例：出勤1日あたり500円の手当を追加して\n"
            "例：危険手当として時給に100円追加して")
        return

    fields = {"タイプ": atype, "手当名": name}
    if parsed.get("amount") is not None:
        fields["金額"] = int(parsed["amount"])
    if parsed.get("rate") is not None:
        fields["割増率(%)"] = float(parsed["rate"])
    if parsed.get("start_month"):
        fields["期間開始月"] = int(parsed["start_month"])
        fields["期間開始日"] = int(parsed.get("start_day", 1))
    if parsed.get("end_month"):
        fields["期間終了月"] = int(parsed["end_month"])
        fields["期間終了日"] = int(parsed.get("end_day", 31))
    if parsed.get("profile"):
        fields["プロファイル名"] = parsed["profile"]
    fields["有効"] = "yes"

    sheets_service.upsert_allowance(user_id, name, fields)

    lines = [f"✅ 手当「{name}」を登録しました。", f"タイプ：{atype}"]
    if "金額" in fields:
        unit = {"月額固定": "円/月", "日数比例": "円/日", "時間単価": "円/時間"}.get(atype, "円")
        lines.append(f"金額：¥{fields['金額']:,}（{unit}）")
    if "割増率(%)" in fields:
        sm, sd = fields.get("期間開始月", "?"), fields.get("期間開始日", "?")
        em, ed = fields.get("期間終了月", "?"), fields.get("期間終了日", "?")
        lines.append(f"割増率：{fields['割増率(%)']}%")
        lines.append(f"対象期間：{sm}/{sd}〜{em}/{ed}")
    if "プロファイル名" in fields:
        lines.append(f"仕事名：{fields['プロファイル名']}")
    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_list_allowances(event, user_id: str):
    allowances = sheets_service.get_allowances(user_id)
    if not allowances:
        line_service.reply_text(event.reply_token,
            "手当が登録されていません。\n"
            "例：バイトリーダー手当を月5000円で追加して")
        return

    lines = ["🎁 カスタム手当一覧"]
    for a in allowances:
        name  = a.get("手当名", "")
        atype = a.get("タイプ", "")
        active = "✅" if str(a.get("有効", "yes")).lower() not in ("no", "false", "0") else "⏸"
        prof = f"（{a['プロファイル名']}）" if a.get("プロファイル名") else ""
        summary = f"{active} {name}{prof}【{atype}】"
        if atype == "月額固定":
            summary += f" ¥{int(a.get('金額', 0)):,}/月"
        elif atype == "日数比例":
            summary += f" ¥{int(a.get('金額', 0)):,}/日"
        elif atype == "時間単価":
            summary += f" ¥{int(a.get('金額', 0)):,}/時間"
        elif atype == "期間割増":
            sm, sd = a.get("期間開始月", "?"), a.get("期間開始日", "?")
            em, ed = a.get("期間終了月", "?"), a.get("期間終了日", "?")
            summary += f" {a.get('割増率(%)', 0)}%割増 {sm}/{sd}〜{em}/{ed}"
        lines.append(summary)
    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_delete_allowance(event, user_id: str, parsed: dict):
    name = (parsed.get("name") or "").strip()
    if not name:
        line_service.reply_text(event.reply_token, "削除する手当名を指定してください。")
        return
    existing = [a for a in sheets_service.get_allowances(user_id) if a.get("手当名") == name]
    if not existing:
        line_service.reply_text(event.reply_token, f"手当「{name}」が見つかりませんでした。")
        return
    _pending_del_allowance[user_id] = {"name": name}
    line_service.reply_with_quickreply(event.reply_token,
        f"手当「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_allowance"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


# ── カスタム控除ハンドラー ───────────────────────────────

_DEDUCTION_TYPE_LABEL = {"固定": "固定", "日数比例": "日数比例", "定率": "定率"}

def _handle_create_custom_deduction(event, user_id: str, parsed: dict):
    name = (parsed.get("name") or "").strip()
    dtype = parsed.get("type", "")
    is_rate = dtype == "定率"
    if not name or dtype not in _DEDUCTION_TYPE_LABEL or (
        parsed.get("rate") is None and parsed.get("amount") is None if is_rate
        else parsed.get("amount") is None
    ):
        line_service.reply_text(event.reply_token,
            "控除名・タイプ・金額（定率の場合は率か今月の金額）を指定してください。\n"
            "例：組合費を固定で月1000円の控除として追加して\n"
            "例：積立金を出勤1日あたり200円の控除として追加して\n"
            "例：子育て支援金を総支給額の0.15%の控除として追加して\n"
            "例：子育て支援金を定率で今月450円引かれた分で登録して")
        return

    fields = {"タイプ": dtype, "控除名": name, "有効": "yes"}
    rate_estimated_from = None
    if is_rate:
        rate = parsed.get("rate")
        if rate is None:
            # %が分からない場合、今月の金額から現在の給与サイクルの総支給額を使って逆算する
            gross = _estimate_current_gross(user_id, parsed.get("profile"))
            if not gross:
                line_service.reply_text(event.reply_token,
                    "今月の総支給額が算出できないため、率を自動計算できませんでした。\n"
                    "締め日の設定・今月のシフト登録を確認するか、率（%）を直接指定してください。\n"
                    "例：子育て支援金を総支給額の0.15%の控除として追加して")
                return
            rate = round(int(parsed["amount"]) / gross * 100, 3)
            rate_estimated_from = (int(parsed["amount"]), gross)
        fields["率(%)"] = float(rate)
    else:
        fields["金額"] = int(parsed["amount"])
    if parsed.get("profile"):
        fields["プロファイル名"] = parsed["profile"]

    sheets_service.upsert_custom_deduction(user_id, name, fields)

    lines = [f"✅ 控除「{name}」を登録しました。", f"タイプ：{dtype}"]
    if is_rate:
        lines.append(f"率：総支給額の{fields['率(%)']:g}%")
        if rate_estimated_from:
            amt, gross = rate_estimated_from
            lines.append(f"（¥{amt:,} ÷ 今月の総支給額¥{gross:,} から自動計算）")
    else:
        unit = {"固定": "円/月", "日数比例": "円/日"}.get(dtype, "円")
        lines.append(f"金額：¥{fields['金額']:,}（{unit}）")
    if "プロファイル名" in fields:
        lines.append(f"仕事名：{fields['プロファイル名']}")
    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_list_custom_deductions(event, user_id: str):
    deductions = sheets_service.get_custom_deductions(user_id)
    if not deductions:
        line_service.reply_text(event.reply_token,
            "カスタム控除が登録されていません。\n"
            "例：組合費を固定で月1000円の控除として追加して")
        return

    lines = ["➖ カスタム控除一覧"]
    for d in deductions:
        name  = d.get("控除名", "")
        dtype = d.get("タイプ", "")
        active = "✅" if str(d.get("有効", "yes")).lower() not in ("no", "false", "0") else "⏸"
        prof = f"（{d['プロファイル名']}）" if d.get("プロファイル名") else ""
        summary = f"{active} {name}{prof}【{dtype}】"
        if dtype == "固定":
            summary += f" ¥{int(d.get('金額', 0)):,}/月"
        elif dtype == "日数比例":
            summary += f" ¥{int(d.get('金額', 0)):,}/日"
        elif dtype == "定率":
            rate = d.get("率(%)", 0)
            summary += f" 総支給額の{float(rate) if rate else 0:g}%"
        lines.append(summary)
    line_service.reply_text(event.reply_token, "\n".join(lines))


def _handle_delete_custom_deduction(event, user_id: str, parsed: dict):
    name = (parsed.get("name") or "").strip()
    if not name:
        line_service.reply_text(event.reply_token, "削除する控除名を指定してください。")
        return
    existing = [d for d in sheets_service.get_custom_deductions(user_id) if d.get("控除名") == name]
    if not existing:
        line_service.reply_text(event.reply_token, f"控除「{name}」が見つかりませんでした。")
        return
    _pending_del_custom_deduction[user_id] = {"name": name}
    line_service.reply_with_quickreply(event.reply_token,
        f"控除「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_custom_deduction"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


# ── 控除データ登録ハンドラー ──────────────────────────

def _handle_register_deductions(event, user_id: str, parsed: dict):
    now = datetime.now(config.TIMEZONE)
    year_month = parsed.get("year_month") or now.strftime("%Y/%m")
    gross = int(parsed.get("gross_salary") or 0)
    if gross <= 0:
        line_service.reply_text(event.reply_token,
            "総支給額を入力してください。\n例：6月の明細 総支給15万 健保7755円 厚生年金13725円 雇用900円 所得税3000円")
        return

    field_map = {
        "健康保険": parsed.get("health_insurance"),
        "介護保険": parsed.get("nursing_insurance"),
        "厚生年金": parsed.get("pension"),
        "雇用保険": parsed.get("employment_insurance"),
        "所得税":   parsed.get("income_tax"),
        "住民税":   parsed.get("resident_tax"),
        "その他":   parsed.get("other"),
    }
    items = {k: int(v) for k, v in field_map.items() if v is not None}
    total_deduction = sum(items.values())
    net = gross - total_deduction

    lines = [f"以下の内容で{year_month}の明細を登録しますか？", f"総支給：¥{gross:,}", "─────────────"]
    for key in ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]:
        if key in items and items[key] > 0:
            lines.append(f"  {key}：¥{items[key]:,}")
    lines += ["─────────────", f"合計控除：¥{total_deduction:,}", f"手取り：¥{net:,}"]

    gs = user_settings.get_settings(user_id)
    active_profile = gs.get("アクティブプロファイル", "") or ""
    existing = sheets_service.get_deduction_by_month(user_id, year_month, active_profile)
    if existing:
        lines.append(
            f"\n⚠️ {year_month}の明細はすでに登録されています。\n"
            f"（総支給¥{int(existing.get('総支給額', 0) or 0):,}）\n"
            f"✏️「登録する」を押すと上書きされます。"
        )
    _pending_deductions[user_id] = {"year_month": year_month, "gross": gross, "items": items, "profile_name": active_profile}
    line_service.reply_with_quickreply(event.reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_deductions"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_delete_deduction(event, user_id: str, parsed: dict):
    now = datetime.now(config.TIMEZONE)
    year_month = parsed.get("year_month") or now.strftime("%Y/%m")
    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]

    existing = sheets_service.get_deduction_by_month(user_id, year_month, profile_name)
    if not existing:
        line_service.reply_text(event.reply_token,
            f"{year_month}の明細データが見つかりませんでした。")
        return

    gross = int(existing.get("総支給額", 0) or 0)
    profile_note = f"（{profile_name}）" if profile_name else ""
    msg = (
        f"🗑️ {year_month}{profile_note}の明細データを削除しますか？\n"
        f"総支給：¥{gross:,}"
    )
    _pending_delete_deductions[user_id] = {"year_month": year_month, "profile_name": profile_name}
    line_service.reply_with_quickreply(event.reply_token, msg, [
        {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_deductions"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_modify_deduction(event, user_id: str, parsed: dict):
    now = datetime.now(config.TIMEZONE)
    year_month = parsed.get("year_month") or now.strftime("%Y/%m")
    field_key = parsed.get("field", "")
    value = parsed.get("value")
    eff = _get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]

    field_map = {
        "gross_salary":          "総支給額",
        "health_insurance":      "健康保険",
        "nursing_insurance":     "介護保険",
        "pension":               "厚生年金",
        "employment_insurance":  "雇用保険",
        "income_tax":            "所得税",
        "resident_tax":          "住民税",
        "other":                 "その他",
    }
    sheet_field = field_map.get(field_key)
    if not sheet_field or value is None:
        line_service.reply_text(event.reply_token,
            "変更する項目と金額を指定してください。\n"
            "例：6月の明細の所得税を5000円に変更して\n"
            "複数項目変更の場合は「6月の明細 総支給16万 健保8000円…」と再登録してください。")
        return

    existing = sheets_service.get_deduction_by_month(user_id, year_month, profile_name)
    if not existing:
        line_service.reply_text(event.reply_token, f"{year_month}の明細が見つかりませんでした。")
        return

    old_val = int(existing.get(sheet_field, 0) or 0)
    profile_note = f"（{profile_name}）" if profile_name else ""
    msg = (
        f"📝 {year_month}{profile_note}の {sheet_field} を変更しますか？\n"
        f"変更前：¥{old_val:,}\n"
        f"変更後：¥{int(value):,}"
    )
    line_service.reply_with_quickreply(event.reply_token, msg, [
        {"label": "✅ 変更する", "data": json.dumps({
            "action": "confirm_modify_deductions",
            "ym": year_month, "field": sheet_field, "value": int(value), "profile": profile_name,
        })},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_update_setting(event, user_id: str, parsed: dict):
    setting_type = parsed.get("setting_type", "")
    value = parsed.get("value")

    if setting_type == "employee_name" and not value:
        _name_input_mode.add(user_id)
        line_service.reply_text(event.reply_token,
            "📝 シフト表に表示されているあなたの名前（フルネーム）を送ってください。\n\n"
            "例：山田太郎"
        )
        return

    if user_settings.update_setting(user_id, setting_type, value):
        if setting_type == "calendar_color":
            _pending_color_update[user_id] = value
            line_service.reply_with_quickreply(event.reply_token,
                f"✅ カレンダーカラーを「{value}」に変更しました。\n\n過去に登録済みのシフトの色も変更しますか？",
                [
                    {"label": "✅ 過去も更新する", "data": json.dumps({"action": "update_past_shifts_color", "apply": True})},
                    {"label": "⏩ 今後のみ適用",   "data": json.dumps({"action": "update_past_shifts_color", "apply": False})},
                ]
            )
            return
        labels = {
            "hourly_wage": f"時給を {value}円 に更新しました。",
            "break_minutes": f"休憩時間を {value}分 に更新しました。",
            "notify_time": f"通知時刻を {value} に更新しました。",
            "notify_enabled": f"通知を {'ON' if str(value).upper() == 'ON' else 'OFF'} にしました。",
            "calendar_title": (
                f"✅ カレンダー予定名を「{value}」に更新しました。\n"
                f"Googleカレンダーに登録されるシフトの予定名がこの名前になります。\n\n"
                f"※ シフト表の写真から自分を見つけるための名前（従業員名）は別の設定です。\n"
                f"従業員名は「仕事名」→「名前を設定」から登録できます。"
            ),
            "employee_name": (
                f"シフト表での名前を「{value}」に設定しました。\n"
                f"次回からシフト表の画像を送ると「{value}」の行を自動で抽出します。"
            ),
            "leave_hours": (
                f"有給の標準時間を {value}時間 に設定しました。\n"
                f"次回から時間の指定なしで有給を取得すると自動で{value}時間分として計算されます。"
            ),
        }
        line_service.reply_text(event.reply_token, labels.get(setting_type, "設定を更新しました。"))
    else:
        line_service.reply_text(event.reply_token, "設定の更新に失敗しました。")


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    reply_token = event.reply_token

    try:
        data = json.loads(event.postback.data)
    except (json.JSONDecodeError, AttributeError):
        return

    try:
        _dispatch_postback(event, data, user_id, reply_token)
    except Exception as e:
        logger.error(f"[handle_postback] 未補足エラー: {e}", exc_info=True)
        try:
            line_service.reply_text(reply_token, "エラーが発生しました。しばらく時間をおいて再度お試しください。")
        except Exception:
            pass


def _dispatch_postback(event, data: dict, user_id: str, reply_token: str) -> None:
    action = data.get("action")

    # Quick Replyボタンタップ時はヘルプ検索モードを解除（再検索ボタンは除く）
    if action != "help_search_again":
        _help_mode.discard(user_id)

    # データ処理を伴うアクションはローディングアニメーションを表示
    _LOADING_ACTIONS = {
        "confirm_register", "confirm_register_multi", "confirm_delete",
        "confirm_update_shift", "confirm_use_leave",
        "confirm_delete_leave", "confirm_modify_leave", "confirm_delete_deductions",
        "confirm_modify_deductions", "confirm_disconnect_calendar", "delete_all_stage2",
        "confirm_delete_allowance", "confirm_delete_profile", "confirm_delete_custom_deduction",
    }
    if action in _LOADING_ACTIONS:
        line_service.send_loading(user_id, seconds=20)

    if action == "confirm_register":
        calendar_title = data.get("ct", config.DEFAULT_CALENDAR_TITLE)
        color_id       = data.get("ci", "")
        date_str   = data.get("date", "")
        start_time = data.get("start", "")
        end_time   = data.get("end", "")
        break_minutes = int(data.get("break", config.DEFAULT_BREAK_MINUTES))
        profile_name = data.get("pn", "")
        work_min = salary_calculator.calc_work_minutes(start_time, end_time, break_minutes)

        # 既存シフトがあれば先に削除（上書き）
        old_eid = sheets_service.delete_shift(user_id, date_str)
        if old_eid and sheets_service.has_google_token(user_id):
            try:
                calendar_service.delete_event(user_id, old_eid)
            except Exception:
                pass
        event_id = ""
        if sheets_service.has_google_token(user_id):
            try:
                event_id = calendar_service.create_event(user_id, date_str, start_time, end_time, summary=calendar_title, color_id=color_id)
            except Exception as e:
                logger.error(f"カレンダー登録エラー: {e}")
        try:
            sheets_service.save_shift(user_id, date_str, start_time, end_time, work_min, event_id, profile_name)
        except Exception as e:
            logger.error(f"シフト保存エラー: {e}")
            line_service.reply_text(reply_token, "登録中にエラーが発生しました。しばらくしてからもう一度お試しください。")
            return
        work_str = salary_calculator.minutes_to_str(work_min)
        cal_note = "\n📅 Googleカレンダー未連携のためスプレッドシートのみ保存" if not event_id else ""
        line_service.reply_text(reply_token,
            f"✅ シフトを{'上書き' if old_eid is not None else ''}登録しました。\n"
            f"📅 {date_str}\n"
            f"⏰ {start_time} 〜 {end_time}\n"
            f"🕐 実働 {work_str}"
            f"{cal_note}"
        )

    elif action == "confirm_register_multi":
        pending = _pending_multi_shifts.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。もう一度送信してください。")
            return

        # 件数が多い場合があるため先に待機メッセージを送信しpushで結果を返す
        line_service.reply_text(reply_token, "⏳ シフトを登録しています。しばらくお待ちください...")

        # batch形式（各シフトが個別条件）か、共通時間形式かを判定
        if "batch" in pending:
            shift_list = pending["batch"]
        else:
            work_min = salary_calculator.calc_work_minutes(pending["start_time"], pending["end_time"], pending["break_minutes"])
            shift_list = [
                {"date": d, "start_time": pending["start_time"], "end_time": pending["end_time"],
                 "break_minutes": pending["break_minutes"], "work_min": work_min,
                 "ct": pending.get("ct", config.DEFAULT_CALENDAR_TITLE),
                 "ci": pending.get("ci", ""), "pn": pending.get("pn", "")}
                for d in pending["dates"]
            ]

        success_items = []
        overwrite_items = []
        fail_dates = []
        for s in shift_list:
            date_str = s["date"]
            calendar_title = s.get("ct", config.DEFAULT_CALENDAR_TITLE)
            color_id = s.get("ci", "")
            profile_name = s.get("pn", "")
            try:
                # 既存シフトがあれば先に削除（上書き）
                old_eid = sheets_service.delete_shift(user_id, date_str)
                if old_eid:
                    if sheets_service.has_google_token(user_id):
                        try:
                            calendar_service.delete_event(user_id, old_eid)
                        except Exception:
                            pass
                    overwrite_items.append(date_str)
                event_id = ""
                if sheets_service.has_google_token(user_id):
                    try:
                        event_id = calendar_service.create_event(user_id, date_str, s["start_time"], s["end_time"], summary=calendar_title, color_id=color_id)
                    except Exception as e:
                        logger.error(f"カレンダー登録エラー ({date_str}): {e}")
                sheets_service.save_shift(user_id, date_str, s["start_time"], s["end_time"], s["work_min"], event_id, profile_name)
                success_items.append(s)
            except Exception as e:
                logger.error(f"一括登録エラー ({date_str}): {e}")
                fail_dates.append(date_str)

        new_count = len(success_items) - len(overwrite_items)
        lines = [f"✅ {len(success_items)}日分のシフトを登録しました。（新規{new_count}日 / 上書き{len(overwrite_items)}日）"]
        for s in success_items:
            work_str = salary_calculator.minutes_to_str(s["work_min"])
            try:
                d = datetime.strptime(s["date"], "%Y/%m/%d")
                weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
                lines.append(f"📅 {s['date']}（{weekday}）{s['start_time']}〜{s['end_time']} 実働{work_str}")
            except ValueError:
                lines.append(f"📅 {s['date']} 実働{work_str}")
        if fail_dates:
            lines.append("\n⚠️ 以下は登録に失敗しました：")
            lines.extend(fail_dates)
        line_service.push_text(user_id, "\n".join(lines))

    elif action == "confirm_delete":
        date_str = data.get("date", "")
        event_id = sheets_service.delete_shift(user_id, date_str)

        if event_id is None:
            line_service.reply_text(reply_token, f"📅 {date_str} のシフトが見つかりませんでした。")
            return

        if event_id and sheets_service.has_google_token(user_id):
            try:
                calendar_service.delete_event(user_id, event_id)
            except Exception as e:
                logger.error(f"カレンダー削除エラー: {e}")

        line_service.reply_text(reply_token, f"🗑️ {date_str} のシフトを削除しました。")

    elif action == "confirm_deductions":
        pending = _pending_deductions.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。もう一度入力してください。")
            return
        # すぐに返信してクイックリプライバーを消す
        line_service.reply_text(reply_token, "⏳ 登録しています...")
        try:
            profile_name = pending.get("profile_name", "")
            sheets_service.save_deduction(
                user_id, pending["year_month"], pending["gross"], pending["items"],
                profile_name, pending.get("deductions_extra", [])
            )
            profile_note = f"（{profile_name}）" if profile_name else ""

            # 有給の自動差し引き（同じ月の明細を再登録した場合、前回分を削除してから反映する）
            paid_leave_days = pending.get("paid_leave_days")
            leave_msg = ""
            year_month_str = pending["year_month"]  # YYYY/MM
            leave_date_str = year_month_str + "/01"
            try:
                sheets_service.delete_leave_usage(user_id, leave_date_str, profile_name)
            except Exception as le:
                logger.warning(f"[confirm_deductions] 既存の有給差し引き削除失敗: {le}")
            if paid_leave_days and float(paid_leave_days) > 0:
                try:
                    sheets_service.use_leave(user_id, leave_date_str, "年次有給", float(paid_leave_days), profile_name)
                    leave_msg = f"\n🌿 有給休暇 {float(paid_leave_days):g}日分を残高から差し引きました。"
                except Exception as le:
                    logger.warning(f"[confirm_deductions] 有給差し引き失敗: {le}")
                    leave_msg = "\n⚠️ 有給の差し引きに失敗しました。手動で確認してください。"

            # 明細から読み取った非標準控除のうち、未登録のものはカスタム控除登録を提案する
            deductions_extra = pending.get("deductions_extra", [])
            existing_ded_names = {d.get("控除名") for d in sheets_service.get_custom_deductions(user_id, profile_name)}
            new_deductions = [d for d in deductions_extra if d["name"] not in existing_ded_names]

            # 明細から読み取った手当のうち、未登録のものはカスタム手当登録を提案する
            all_allowances = pending.get("allowances", [])
            existing_allowance_names = {a.get("手当名") for a in sheets_service.get_allowances(user_id, profile_name)}
            allowances = [a for a in all_allowances if a["name"] not in existing_allowance_names]

            # 勤務日数を取得して日数比例の1日単価を計算（控除・手当共通）
            work_days = 0
            if new_deductions or allowances:
                import calendar as _cal
                year_month = pending["year_month"]
                try:
                    ym = datetime.strptime(year_month, "%Y/%m")
                    last_day = _cal.monthrange(ym.year, ym.month)[1]
                    month_shifts = sheets_service.get_shifts_in_period(
                        user_id, f"{year_month}/01", f"{year_month}/{last_day:02d}", profile_name
                    )
                    work_days = len(month_shifts)
                except Exception:
                    work_days = 0

            base_msg = f"✅ {pending['year_month']}の明細{profile_note}を登録しました。{leave_msg}"

            if new_deductions:
                enriched = []
                for d in new_deductions:
                    entry = dict(d)
                    entry["per_day"] = round(d["amount"] / work_days) if work_days > 0 else None
                    entry["rate"] = round(d["amount"] / pending["gross"] * 100, 3) if pending.get("gross") else None
                    enriched.append(entry)
                _pending_payslip_deductions[user_id] = {
                    "items": enriched, "profile_name": profile_name,
                    "allowances": allowances, "work_days": work_days,
                }
                _offer_next_payslip_deduction(user_id, base_msg)
            elif allowances:
                _offer_payslip_allowances(user_id, base_msg, allowances, profile_name, work_days)
            else:
                line_service.push_text(user_id, f"{base_msg}\n次回の給与確認から手取り予測に反映されます。")
        except Exception as e:
            logger.error(f"[confirm_deductions] エラー: {e}")
            line_service.push_text(user_id, "登録中にエラーが発生しました。もう一度お試しください。")

    elif action == "delete_all_stage1":
        # 第2段階確認（最終確認）
        line_service.reply_with_quickreply(reply_token,
            "🚨 最終確認（2/2）\n\n"
            "本当にすべてのデータを完全に削除しますか？\n\n"
            "削除後は元に戻すことができません。\n"
            "Googleカレンダーとの連携は解除されますが、カレンダー上の予定はそのまま残ります。", [
                {"label": "🗑️ 完全に削除する", "data": json.dumps({"action": "delete_all_stage2"})},
                {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
            ])

    elif action == "delete_all_stage2":
        # データ量が多い場合があるため先に待機メッセージを送信しpushで結果を返す
        line_service.reply_text(reply_token, "⏳ データを削除しています。しばらくお待ちください...")
        try:
            # スプレッドシートの全データ削除（トークンも同時にクリア）
            sheets_service.delete_all_user_data(user_id)

            msg_parts = ["✅ 全データを削除しました。\n"]
            msg_parts.append("・シフトデータ・明細・有給・仕事名設定を削除しました。")
            msg_parts.append("・Googleカレンダーとの連携を解除しました。")
            msg_parts.append("（カレンダー上の予定はそのまま残ります）")
            msg_parts.append("\nまたご利用の際はメッセージを送ってください。")
            line_service.push_text(user_id, "\n".join(msg_parts))

        except Exception as e:
            logger.error(f"全データ削除エラー: {e}")
            line_service.push_text(user_id,
                "削除中にエラーが発生しました。一部のデータが残っている可能性があります。")

    elif action == "confirm_disconnect_calendar":
        if sheets_service.clear_google_tokens(user_id):
            line_service.reply_text(reply_token,
                "🔓 Googleカレンダーとの連携を解除しました。\n"
                "シフトデータはスプレッドシートに引き続き保存されます。\n"
                "再連携するには「Googleカレンダーを連携する」と送ってください。")
        else:
            line_service.reply_text(reply_token, "連携解除に失敗しました。")

    elif action == "confirm_delete_deductions":
        pending = _pending_delete_deductions.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "削除データが見つかりませんでした。もう一度入力してください。")
            return
        year_month = pending["year_month"]
        profile_name = pending.get("profile_name", "")
        if sheets_service.delete_deduction(user_id, year_month, profile_name):
            profile_note = f"（{profile_name}）" if profile_name else ""
            line_service.reply_text(reply_token, f"🗑️ {year_month}{profile_note}の明細データを削除しました。")
        else:
            line_service.reply_text(reply_token, "削除に失敗しました。データが見つかりませんでした。")

    elif action == "confirm_modify_deductions":
        year_month = data.get("ym", "")
        field = data.get("field", "")
        value = data.get("value")
        profile_name = data.get("profile", "")
        if not year_month or not field or value is None:
            line_service.reply_text(reply_token, "変更データが不正です。もう一度入力してください。")
            return
        if sheets_service.update_deduction_field(user_id, year_month, field, int(value), profile_name):
            profile_note = f"（{profile_name}）" if profile_name else ""
            line_service.reply_text(reply_token,
                f"✅ {year_month}{profile_note}の {field} を ¥{int(value):,} に変更しました。")
        else:
            line_service.reply_text(reply_token, "変更に失敗しました。データが見つかりませんでした。")

    elif action == "select_delete_allowance":
        name = data.get("name", "")
        if not any(a.get("手当名") == name for a in sheets_service.get_allowances(user_id)):
            line_service.reply_text(reply_token, f"手当「{name}」が見つかりませんでした。")
            return
        _pending_del_allowance[user_id] = {"name": name}
        line_service.reply_with_quickreply(reply_token,
            f"手当「{name}」を削除しますか？",
            [
                {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_allowance"})},
                {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
            ]
        )

    elif action == "confirm_delete_allowance":
        name = _pending_del_allowance.pop(user_id, {}).get("name", "")
        if name and sheets_service.delete_allowance(user_id, name):
            line_service.reply_text(reply_token, f"🗑️ 手当「{name}」を削除しました。")
        else:
            line_service.reply_text(reply_token, "削除に失敗しました。")

    elif action == "select_delete_custom_deduction":
        name = data.get("name", "")
        if not any(d.get("控除名") == name for d in sheets_service.get_custom_deductions(user_id)):
            line_service.reply_text(reply_token, f"控除「{name}」が見つかりませんでした。")
            return
        _pending_del_custom_deduction[user_id] = {"name": name}
        line_service.reply_with_quickreply(reply_token,
            f"控除「{name}」を削除しますか？",
            [
                {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_custom_deduction"})},
                {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
            ]
        )

    elif action == "confirm_delete_custom_deduction":
        name = _pending_del_custom_deduction.pop(user_id, {}).get("name", "")
        if name and sheets_service.delete_custom_deduction(user_id, name):
            line_service.reply_text(reply_token, f"🗑️ 控除「{name}」を削除しました。")
        else:
            line_service.reply_text(reply_token, "削除に失敗しました。")

    elif action == "select_delete_profile":
        name = data.get("name", "")
        if not sheets_service.get_profile(user_id, name):
            line_service.reply_text(reply_token, f"仕事名「{name}」が見つかりませんでした。")
            return
        line_service.reply_with_quickreply(reply_token,
            f"⚠️ 仕事名「{name}」削除の確認（1/2）\n\n"
            f"以下のデータがすべて削除されます：\n"
            f"・仕事名の設定（時給・休憩時間・締め日など）\n"
            f"・この仕事名で登録したシフトデータ\n"
            f"・この仕事名の給与明細・控除データ\n"
            f"・この仕事名専用のカスタム手当\n"
            f"・この仕事名専用のカスタム控除\n\n"
            f"※ Googleカレンダー上の予定はそのまま残ります（削除されません）。\n"
            f"この操作は取り消せません。\n"
            f"本当に続けますか？",
            [
                {"label": "続ける", "data": json.dumps({"action": "delete_profile_stage1", "name": name})},
                {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
            ]
        )

    elif action == "delete_profile_stage1":
        name = data.get("name", "")
        line_service.reply_with_quickreply(reply_token,
            f"🚨 最終確認（2/2）\n\n"
            f"本当に仕事名「{name}」と関連データを完全に削除しますか？\n\n"
            f"削除後は元に戻すことができません。",
            [
                {"label": "🗑️ 完全に削除する", "data": json.dumps({"action": "confirm_delete_profile", "name": name})},
                {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
            ]
        )

    elif action == "confirm_delete_profile":
        name = data.get("name", "")
        line_service.reply_text(reply_token, "⏳ 削除しています...")
        try:
            if sheets_service.delete_profile(user_id, name):
                gs = user_settings.get_settings(user_id)
                if gs.get("アクティブプロファイル") == name:
                    user_settings.update_setting(user_id, "active_profile", "")
                line_service.push_text(user_id,
                    f"✅ 仕事名「{name}」と関連データを削除しました。\n"
                    f"・仕事名の設定\n・シフトデータ\n・給与明細・控除データ\n・カスタム手当\n・カスタム控除\n"
                    f"（Googleカレンダー上の予定はそのまま残ります）")
            else:
                line_service.push_text(user_id, f"仕事名「{name}」が見つかりませんでした。")
        except Exception as e:
            logger.error(f"仕事名削除エラー: {e}")
            line_service.push_text(user_id, "削除中にエラーが発生しました。一部のデータが残っている可能性があります。")

    elif action == "switch_profile_direct":
        name = data.get("name", "")
        if not sheets_service.get_profile(user_id, name):
            line_service.reply_text(reply_token, f"仕事名「{name}」が見つかりません。\nまず登録してください。")
            return
        user_settings.update_setting(user_id, "active_profile", name)
        line_service.reply_text(reply_token,
            f"✅ 「{name}」に切り替えました。\n以降のシフト登録はこの仕事名の設定が使われます。")

    elif action == "confirm_use_leave":
        pending = _pending_leave_usage.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "取得データが見つかりませんでした。もう一度入力してください。")
            return

        date_str = pending["date"]
        leave_type = pending["type"]
        days = pending["days"]
        start_time = pending.get("start_time")
        end_time = pending.get("end_time")
        work_min = pending.get("work_min", 0)
        profile_name = pending.get("profile_name", "")

        # 既存シフト・有給記録があれば先に削除（上書き）
        old_eid = sheets_service.delete_shift(user_id, date_str)
        if old_eid:
            try:
                calendar_service.delete_event(user_id, old_eid)
            except Exception:
                pass
        sheets_service.delete_leave_usage(user_id, date_str, profile_name)

        sheets_service.use_leave(user_id, date_str, leave_type, days, profile_name)

        is_overwrite = old_eid is not None
        msg_parts = [f"✅ {date_str}の{leave_type}（{'1日' if days == 1.0 else '半日'}）を{'上書き' if is_overwrite else ''}記録しました。"]
        if profile_name:
            msg_parts.append(f"仕事名：{profile_name}")

        if start_time and end_time and work_min > 0:
            # タイトル：プロファイルのカレンダータイトルを使用（例：有給（バイトA））
            calendar_title = pending.get("calendar_title", "")
            event_title = f"有給（{calendar_title}）" if calendar_title else f"有給（{leave_type}）"
            event_id = ""
            # カレンダー登録は連携済みの場合のみ（終日予定として登録）
            if sheets_service.has_google_token(user_id):
                try:
                    color_id = _resolve_color(pending.get("color", ""))
                    event_id = calendar_service.create_allday_event(
                        user_id, date_str,
                        summary=event_title, color_id=color_id,
                    )
                    msg_parts.append("Googleカレンダーに終日予定として追加しました。")
                except Exception as e:
                    logger.error(f"有給カレンダー登録エラー: {e}")
            # シフトデータへの保存は常に実施（給与計算のため）
            try:
                sheets_service.save_shift(user_id, date_str, start_time, end_time, work_min, event_id, profile_name)
                work_str = salary_calculator.minutes_to_str(work_min)
                msg_parts.append(f"⏰ {start_time}〜{end_time}（実働{work_str}→給与に反映）")
            except Exception as e:
                logger.error(f"有給シフトデータ保存エラー: {e}")

        line_service.reply_text(reply_token, "\n".join(msg_parts))

    elif action == "confirm_update_shift":
        pending = _pending_update_shifts.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "変更データが見つかりませんでした。もう一度入力してください。")
            return

        date_str = pending["date"]
        new_start = pending["start"]
        new_end = pending["end"]
        work_min = pending["work_min"]

        event_id = sheets_service.update_shift(user_id, date_str, new_start, new_end, work_min)
        if event_id is None:
            line_service.reply_text(reply_token, f"📅 {date_str} のシフトが見つかりませんでした。")
            return

        if event_id and sheets_service.has_google_token(user_id):
            eff = _get_effective_settings(user_id, None)
            try:
                calendar_service.update_event(
                    user_id, event_id, date_str, new_start, new_end,
                    summary=eff["calendar_title"], color_id=_resolve_color(eff["color"]),
                )
            except Exception as e:
                logger.error(f"カレンダー更新エラー: {e}")

        work_str = salary_calculator.minutes_to_str(work_min)
        line_service.reply_text(reply_token,
            f"✅ シフトを変更しました。\n"
            f"📅 {date_str}\n"
            f"⏰ {new_start} 〜 {new_end}\n"
            f"🕐 実働 {work_str}"
        )

    elif action == "confirm_delete_leave":
        pending = _pending_delete_leave.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "削除データが見つかりませんでした。もう一度入力してください。")
            return

        date_str = pending["date"]
        profile_name = pending.get("profile_name", "")
        event_id = pending.get("event_id", "")

        # カレンダー終日予定を削除
        if event_id:
            try:
                calendar_service.delete_event(user_id, event_id)
            except Exception as e:
                logger.error(f"有給カレンダー削除エラー: {e}")

        # シフトデータから削除
        sheets_service.delete_shift(user_id, date_str)

        # 有給管理の使用記録を削除（残日数を戻す）
        deleted = sheets_service.delete_leave_usage(user_id, date_str, profile_name)

        leave_type = deleted.get("種類", "年次有給") if deleted else "年次有給"
        msg_parts = [f"🗑️ {date_str}の{leave_type}を削除しました。"]
        if deleted:
            days = float(deleted.get("日数", 1) or 1)
            msg_parts.append(f"残日数に{days:g}日分が戻りました。")
        if event_id:
            msg_parts.append("Googleカレンダーからも削除しました。")
        line_service.reply_text(reply_token, "\n".join(msg_parts))

    elif action == "confirm_modify_leave":
        pending = _pending_modify_leave.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "変更データが見つかりませんでした。もう一度入力してください。")
            return

        date_str = pending["date"]
        profile_name = pending.get("profile_name", "")
        event_id = pending.get("event_id", "")
        eff = pending["eff"]
        new_days = pending.get("new_days")
        new_type = pending.get("new_type")

        # 旧記録を削除
        if event_id:
            try:
                calendar_service.delete_event(user_id, event_id)
            except Exception as e:
                logger.error(f"有給カレンダー削除エラー: {e}")
        old_leave = sheets_service.delete_leave_usage(user_id, date_str, profile_name)
        sheets_service.delete_shift(user_id, date_str)

        # 新しい値を確定
        leave_type = new_type or (old_leave.get("種類", "年次有給") if old_leave else "年次有給")
        days = new_days if new_days is not None else float(old_leave.get("日数", 1) or 1) if old_leave else 1.0
        old_start = pending.get("old_start") or eff["default_start"]
        old_end = pending.get("old_end") or eff["default_end"]
        leave_hours = eff["leave_hours"]

        if old_start and old_end:
            break_min = eff["break_minutes"] if days == 1.0 else 0
            work_min = salary_calculator.calc_work_minutes(old_start, old_end, break_min)
            start_time, end_time = old_start, old_end
        elif leave_hours is not None:
            total_hours = leave_hours * days
            work_min = int(total_hours * 60)
            start_time = "09:00"
            end_time = (datetime.strptime("09:00", "%H:%M") + timedelta(hours=total_hours)).strftime("%H:%M")
        else:
            work_min = 0
            start_time = end_time = None

        # 新しい有給記録を登録
        sheets_service.use_leave(user_id, date_str, leave_type, days, profile_name)
        new_event_id = ""
        if sheets_service.has_google_token(user_id):
            try:
                calendar_title = eff.get("calendar_title", "")
                ev_title = f"有給（{calendar_title}）" if calendar_title else f"有給（{leave_type}）"
                color_id = _resolve_color(eff.get("color", ""))
                new_event_id = calendar_service.create_allday_event(
                    user_id, date_str, summary=ev_title, color_id=color_id,
                )
            except Exception as e:
                logger.error(f"有給カレンダー再登録エラー: {e}")
        if start_time and end_time and work_min > 0:
            sheets_service.save_shift(user_id, date_str, start_time, end_time, work_min, new_event_id, profile_name)

        day_label = "1日" if days == 1.0 else "半日（0.5日）"
        line_service.reply_text(reply_token,
            f"✅ {date_str}の有給を変更しました。\n"
            f"種類：{leave_type}\n"
            f"取得日数：{day_label}"
        )

    elif action == "help_category":
        cat = data.get("cat", "")
        if cat == "settings":
            line_service.reply_with_quickreply(reply_token, _build_settings_help(user_id),
                _settings_qr() + [{"label": "📖 カテゴリ一覧", "data": json.dumps({"action": "help_show_all"})}]
            )
        elif cat:
            items = help_content.get_category_items(cat)
            text  = help_content.get_category_text(cat)
            qr = []
            for item in items:
                label = item["title"][:20]
                qr.append({"label": label, "data": json.dumps({"action": "help_item", "id": item["id"]})})
            qr.append({"label": "◀ カテゴリ一覧", "data": json.dumps({"action": "help_show_all"})})
            qr.append({"label": "🔍 キーワード検索", "data": json.dumps({"action": "help_search_again"})})
            line_service.reply_with_quickreply(reply_token, text, qr)

    elif action == "help_item":
        item_id = data.get("id", "")
        item = help_content.get_by_id(item_id)
        if item:
            detail = _build_settings_help(user_id) if item.get("detail") == "__LIVE_SETTINGS__" else item["detail"]
            back_cat = item.get("category", "")
            line_service.reply_with_quickreply(reply_token, detail, [
                {"label": "◀ カテゴリに戻る", "data": json.dumps({"action": "help_category", "cat": back_cat})},
                {"label": "🔍 再検索",         "data": json.dumps({"action": "help_search_again"})},
                {"label": "📖 カテゴリ一覧",   "data": json.dumps({"action": "help_show_all"})},
            ])
        else:
            line_service.reply_text(reply_token, "項目が見つかりませんでした。")

    elif action == "help_show_all":
        cats = help_content.get_category_list()
        qr = [{"label": c["label"], "data": json.dumps({"action": "help_category", "cat": c["cat"]})} for c in cats]
        qr.append({"label": "🔍 キーワード検索", "data": json.dumps({"action": "help_search_again"})})
        line_service.reply_with_quickreply(reply_token, help_content.get_all_text(), qr)

    elif action == "help_search_again":
        _help_mode.add(user_id)
        line_service.reply_text(reply_token,
            "🔍 調べたいキーワードを送ってください。\n"
            "（例：シフト、有給、給与、仕事名...）\n\n"
            "「一覧」と送ると全機能を表示します。"
        )

    elif action == "cancel":
        line_service.reply_text(reply_token, "キャンセルしました。")

    elif action == "skip_allowance":
        _pending_payslip_allowances.pop(user_id, None)
        line_service.reply_text(reply_token, "手当の登録をスキップしました。\n後から「手当を追加して」と送っていつでも登録できます。")

    elif action == "select_shift_name":
        pending = _pending_name_selection.pop(user_id, None)
        if not pending:
            line_service.reply_text(reply_token, "選択データが見つかりません。もう一度画像を送ってください。")
            return
        selected_name = data.get("name", "")
        b64           = pending.get("b64")
        structure     = pending.get("structure")
        original_name = pending.get("employee_name", "")

        if not b64 or not structure:
            line_service.reply_text(reply_token, "画像データが見つかりません。もう一度画像を送ってください。")
            return

        # reply_token は最終返信で1回だけ使う。待機中はローディングアニメーションを使用
        line_service.send_loading(user_id, seconds=20)

        if selected_name:
            # 選択された名前で再解析
            user_settings.update_setting(user_id, "ocr_name", selected_name)
            result = shift_parser.reparse_with_name(b64, structure, selected_name, original_name)
            matched = result.get("shifts", [])
            display_name = selected_name
        else:
            # 全員分を表示
            matched = shift_parser.reparse_all_employees(b64, structure)
            display_name = ""

        if not matched:
            line_service.reply_text(reply_token, "シフトが見つかりませんでした。もう一度画像を送ってください。")
            return

        _send_shift_confirm(reply_token, user_id, matched, display_name)

    elif action == "select_payslip_allowance":
        pending = _pending_payslip_allowances.get(user_id)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。")
            return
        idx = data.get("idx", 0)
        allowances = pending["allowances"]
        if idx >= len(allowances):
            line_service.reply_text(reply_token, "手当データが見つかりませんでした。")
            return
        a = allowances[idx]
        per_day = a.get("per_day")

        qr = [{"label": f"💰 固定 ¥{a['amount']:,}/月", "data": json.dumps({"action": "register_payslip_allowance", "idx": idx, "type": "月額固定"})}]
        if per_day:
            qr.insert(0, {"label": f"📅 日数比例 ¥{per_day:,}/日", "data": json.dumps({"action": "register_payslip_allowance", "idx": idx, "type": "日数比例"})})
        qr.append({"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})})

        note = f"（今月の勤務{pending.get('work_days', 0)}日から1日単価を計算）" if per_day else "（勤務日数が取得できないため固定のみ選べます）"
        line_service.reply_with_quickreply(reply_token,
            f"「{a['name']}」（¥{a['amount']:,}）をどちらのタイプで登録しますか？\n{note}",
            qr
        )

    elif action == "register_payslip_allowance":
        pending = _pending_payslip_allowances.get(user_id)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。")
            return
        idx = data.get("idx", 0)
        atype = data.get("type", "月額固定")
        allowances = pending["allowances"]
        if idx >= len(allowances):
            line_service.reply_text(reply_token, "手当データが見つかりませんでした。")
            return
        a = allowances[idx]
        profile_name = pending.get("profile_name", "")
        per_day = a.get("per_day")

        if atype == "日数比例" and per_day:
            fields = {"タイプ": "日数比例", "金額": per_day, "プロファイル名": profile_name, "有効": "yes"}
            reg_desc = f"日数比例 ¥{per_day:,}/日"
        else:
            fields = {"タイプ": "月額固定", "金額": a["amount"], "プロファイル名": profile_name, "有効": "yes"}
            reg_desc = f"月額固定 ¥{a['amount']:,}/月"

        sheets_service.upsert_allowance(user_id, a["name"], fields)

        # 残りの手当があれば続けて提案
        remaining = [x for i, x in enumerate(allowances) if i != idx]
        _pending_payslip_allowances[user_id]["allowances"] = remaining
        if remaining:
            qr = []
            for i, r in enumerate(remaining[:11]):
                qr.append({"label": f"✅ {r['name'][:10]}", "data": json.dumps({"action": "select_payslip_allowance", "idx": i})})
            qr.append({"label": "⏭️ スキップ", "data": json.dumps({"action": "skip_allowance"})})
            line_service.reply_with_quickreply(reply_token,
                f"✅ 「{a['name']}」を{reg_desc}で登録しました。\n\n他の手当も登録しますか？",
                qr
            )
        else:
            _pending_payslip_allowances.pop(user_id, None)
            line_service.reply_text(reply_token,
                f"✅ 「{a['name']}」を{reg_desc}のカスタム手当として登録しました。\n"
                "給与予測に毎月反映されます。\n\n"
                "手当の確認・変更は「手当一覧」から行えます。"
            )

    elif action == "select_payslip_deduction":
        pending = _pending_payslip_deductions.get(user_id)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。")
            return
        idx = data.get("idx", 0)
        items = pending["items"]
        if idx >= len(items):
            line_service.reply_text(reply_token, "控除データが見つかりませんでした。")
            return
        d = items[idx]
        per_day = d.get("per_day")
        rate = d.get("rate")

        qr = [{"label": f"💰 固定 ¥{d['amount']:,}/月", "data": json.dumps({"action": "register_payslip_deduction", "idx": idx, "type": "固定"})}]
        if per_day:
            qr.insert(0, {"label": f"📅 日数比例 ¥{per_day:,}/日", "data": json.dumps({"action": "register_payslip_deduction", "idx": idx, "type": "日数比例"})})
        if rate:
            qr.insert(0, {"label": f"📊 定率 総支給の{rate:g}%", "data": json.dumps({"action": "register_payslip_deduction", "idx": idx, "type": "定率"})})
        qr.append({"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})})

        note = "（今月の勤務日数・総支給額から自動計算した候補です。金額が変わらない実費系なら固定、出勤日数に応じて変わるなら日数比例、総支給額の◯%で決まる保険料的な控除なら定率を選んでください）"
        line_service.reply_with_quickreply(reply_token,
            f"「{d['name']}」（¥{d['amount']:,}）をどのタイプで登録しますか？\n{note}",
            qr
        )

    elif action == "register_payslip_deduction":
        pending = _pending_payslip_deductions.get(user_id)
        if not pending:
            line_service.reply_text(reply_token, "登録データが見つかりませんでした。")
            return
        idx = data.get("idx", 0)
        dtype = data.get("type", "固定")
        items = pending["items"]
        if idx >= len(items):
            line_service.reply_text(reply_token, "控除データが見つかりませんでした。")
            return
        d = items[idx]
        profile_name = pending.get("profile_name", "")
        per_day = d.get("per_day")
        rate = d.get("rate")

        if dtype == "日数比例" and per_day:
            fields = {"タイプ": "日数比例", "金額": per_day, "プロファイル名": profile_name, "有効": "yes"}
            reg_desc = f"日数比例 ¥{per_day:,}/日"
        elif dtype == "定率" and rate:
            fields = {"タイプ": "定率", "率(%)": rate, "プロファイル名": profile_name, "有効": "yes"}
            reg_desc = f"定率 総支給額の{rate:g}%"
        else:
            fields = {"タイプ": "固定", "金額": d["amount"], "プロファイル名": profile_name, "有効": "yes"}
            reg_desc = f"固定 ¥{d['amount']:,}/月"

        sheets_service.upsert_custom_deduction(user_id, d["name"], fields)

        # 残りの控除項目があれば続けて提案
        remaining = [x for i, x in enumerate(items) if i != idx]
        pending["items"] = remaining
        if remaining:
            qr = []
            for i, r in enumerate(remaining[:11]):
                qr.append({"label": f"✅ {r['name'][:10]}", "data": json.dumps({"action": "select_payslip_deduction", "idx": i})})
            qr.append({"label": "⏭️ スキップ", "data": json.dumps({"action": "skip_payslip_deductions"})})
            line_service.reply_with_quickreply(reply_token,
                f"✅ 「{d['name']}」を{reg_desc}で登録しました。\n\n他の控除も登録しますか？",
                qr
            )
        else:
            line_service.reply_text(reply_token,
                f"✅ 「{d['name']}」を{reg_desc}のカスタム控除として登録しました。\n"
                "給与予測に毎月反映されます。\n\n"
                "控除の確認・変更は「控除一覧」から行えます。"
            )
            _offer_next_payslip_deduction(user_id)

    elif action == "skip_payslip_deductions":
        pending = _pending_payslip_deductions.get(user_id)
        if pending:
            pending["items"] = []
        line_service.reply_text(reply_token, "控除の登録をスキップしました。\n後から「控除を追加して」と送っていつでも登録できます。")
        _offer_next_payslip_deduction(user_id)

    elif action == "close_menu":
        pass  # クイックリプライを閉じるだけ。返信なし。

    elif action == "connect_calendar":
        _handle_connect_calendar_postback(reply_token, user_id)

    elif action == "disconnect_calendar":
        _handle_disconnect_calendar_postback(reply_token, user_id)

    # ── 設定変更アクション ────────────────────────────────
    elif action == "setting_input_start":
        setting = data.get("setting", "")
        if setting == "employee_name":
            _name_input_mode.add(user_id)
            line_service.reply_text(reply_token,
                "📝 シフト表に表示されているあなたの名前（フルネーム）を送ってください。\n\n例：山田太郎"
            )
        elif setting in _SETTING_PROMPTS:
            _setting_input_mode[user_id] = setting
            prompt, example = _SETTING_PROMPTS[setting]
            line_service.reply_text(reply_token, f"{prompt}\n\n{example}")
        else:
            line_service.reply_text(reply_token, "不明な設定項目です。")

    elif action == "setting_notify_menu":
        gs = user_settings.get_settings(user_id)
        current_time = gs.get("通知時刻", "未設定")
        current_on   = gs.get("通知ON/OFF", "")
        status = f"現在：{'ON' if current_on == 'ON' else 'OFF'}（{current_time}）"
        line_service.reply_with_quickreply(reply_token,
            f"🔔 通知設定\n{status}\n\nON/OFFの切り替えや時刻変更ができます。",
            [
                {"label": "✅ ONにする",   "data": json.dumps({"action": "setting_set_value", "setting": "notify_enabled", "value": "ON"})},
                {"label": "🔕 OFFにする", "data": json.dumps({"action": "setting_set_value", "setting": "notify_enabled", "value": "OFF"})},
                {"label": "⏰ 時刻を変更", "data": json.dumps({"action": "setting_input_start", "setting": "notify_time"})},
                {"label": "◀ 設定一覧",   "data": json.dumps({"action": "setting_show"})},
            ]
        )

    elif action == "setting_calendar_menu":
        gs = user_settings.get_settings(user_id)
        current_title = gs.get("カレンダータイトル", "") or "シフト（デフォルト）"
        current_color = gs.get("カレンダーカラー", "") or "未設定"
        has_cal = sheets_service.has_google_token(user_id)
        cal_status = "連携済み ✅" if has_cal else "未連携"
        cal_button = (
            {"label": "🔓 連携を解除", "data": json.dumps({"action": "disconnect_calendar"})}
            if has_cal else
            {"label": "🔗 Googleカレンダー連携", "data": json.dumps({"action": "connect_calendar"})}
        )
        line_service.reply_with_quickreply(reply_token,
            f"🗓️ カレンダー設定\n"
            f"予定名：{current_title}\n"
            f"カラー：{current_color}\n"
            f"Google連携：{cal_status}",
            [
                {"label": "📌 予定名を変更",      "data": json.dumps({"action": "setting_input_start", "setting": "calendar_title"})},
                {"label": "🎨 カラーを変更",      "data": json.dumps({"action": "setting_color_menu"})},
                cal_button,
                {"label": "◀ 設定一覧",           "data": json.dumps({"action": "setting_show"})},
            ]
        )

    elif action == "setting_color_menu":
        qr = [
            {"label": color, "data": json.dumps({"action": "setting_set_value", "setting": "calendar_color", "value": color})}
            for color in _CALENDAR_COLORS
        ]
        qr.append({"label": "◀ カレンダー設定", "data": json.dumps({"action": "setting_calendar_menu"})})
        line_service.reply_with_quickreply(reply_token,
            "🎨 カレンダーのカラーを選んでください。",
            qr
        )

    elif action == "setting_social_menu":
        gs = user_settings.get_settings(user_id)
        current = gs.get("社会保険加入", "なし") or "なし"
        line_service.reply_with_quickreply(reply_token,
            f"🏥 社会保険の加入状況を設定してください。\n現在：{current}",
            [
                {"label": "✅ 加入あり",  "data": json.dumps({"action": "setting_set_value", "setting": "social_insurance", "value": "あり"})},
                {"label": "❌ 加入なし",  "data": json.dumps({"action": "setting_set_value", "setting": "social_insurance", "value": "なし"})},
                {"label": "◀ 設定一覧",  "data": json.dumps({"action": "setting_show"})},
            ]
        )

    elif action == "setting_set_value":
        setting = data.get("setting", "")
        value   = data.get("value")
        setting_map = {
            "notify_enabled":  "notify_enabled",
            "calendar_color":  "calendar_color",
            "social_insurance": "social_insurance",
        }
        if setting in setting_map and value is not None:
            user_settings.update_setting(user_id, setting_map[setting], value)
            if setting == "calendar_color":
                _pending_color_update[user_id] = value
                line_service.reply_with_quickreply(reply_token,
                    f"✅ カレンダーカラーを「{value}」に変更しました。\n\n過去に登録済みのシフトの色も変更しますか？",
                    [
                        {"label": "✅ 過去も更新する", "data": json.dumps({"action": "update_past_shifts_color", "apply": True})},
                        {"label": "⏩ 今後のみ適用",   "data": json.dumps({"action": "update_past_shifts_color", "apply": False})},
                    ]
                )
            elif setting == "notify_enabled":
                line_service.reply_with_quickreply(reply_token,
                    f"✅ 通知を {value} にしました。",
                    [
                        {"label": "⏰ 時刻を変更",  "data": json.dumps({"action": "setting_input_start", "setting": "notify_time"})},
                        {"label": "◀ 設定一覧",     "data": json.dumps({"action": "setting_show"})},
                    ]
                )
            else:
                line_service.reply_with_quickreply(reply_token,
                    f"✅ 社会保険の加入状況を「{value}」に変更しました。",
                    _settings_qr()
                )

    elif action == "update_past_shifts_color":
        apply      = data.get("apply", False)
        color_name = _pending_color_update.pop(user_id, None)
        if not apply or not color_name:
            line_service.reply_with_quickreply(reply_token,
                "今後登録するシフトから新しい色が適用されます。",
                [{"label": "📋 設定一覧", "data": json.dumps({"action": "setting_show"})}]
            )
            return
        color_id = _resolve_color(color_name)
        shifts   = sheets_service.get_all_shifts_with_event_id(user_id)
        updated  = 0
        failed   = 0
        for shift in shifts:
            event_id = shift.get("Calendar EventID", "").strip()
            if event_id:
                ok = calendar_service.patch_event_color(user_id, event_id, color_id)
                if ok:
                    updated += 1
                else:
                    failed += 1
        msg = f"✅ 過去のシフト {updated}件 の色を「{color_name}」に更新しました。"
        if failed:
            msg += f"\n⚠️ {failed}件 は更新できませんでした（削除済みのイベント等）。"
        line_service.reply_with_quickreply(reply_token, msg,
            [{"label": "📋 設定一覧", "data": json.dumps({"action": "setting_show"})}]
        )

    elif action == "setting_show":
        line_service.reply_with_quickreply(
            reply_token,
            user_settings.format_settings(user_settings.get_settings(user_id)),
            _settings_qr()
        )

    elif action == "menu_help":
        _handle_help(event, user_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
