import json
import logging
from datetime import datetime

from flask import Flask, request, abort, redirect
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, ImageMessageContent
from apscheduler.schedulers.background import BackgroundScheduler

import config
import sheets_service
import shift_parser
import user_settings
import notification
import line_service
import oauth_service
import state
from handlers import (
    leave_handlers,
    payslip_handlers,
    profile_handlers,
    salary_handlers,
    settings_handlers,
    shift_handlers,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
handler = WebhookHandler(config.LINE_CHANNEL_SECRET)

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
    state_param = request.args.get("state", "")
    code = request.args.get("code", "")

    user_id = oauth_service.handle_callback(state_param, code)
    if not user_id:
        return state.OAUTH_ERROR_HTML, 400

    try:
        line_service.push_text(
            user_id,
            "✅ Googleカレンダーとの連携が完了しました！\n再度シフトを送信してください。"
        )
    except Exception as e:
        logger.error(f"連携完了通知の送信失敗: {e}")

    return state.OAUTH_SUCCESS_HTML


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


def _dispatch_message(event, user_id: str, text: str, reply_token: str) -> None:
    # リッチメニューボタン押下：GPT不要で直接Quick Replyを返す
    if text in state.RICH_MENU_REPLIES:
        menu = state.RICH_MENU_REPLIES[text]
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
        state.name_input_mode.add(user_id)
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
        state.leave_grant_input_mode.add(user_id)
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
        shift_handlers._handle_list(event, user_id, {})
        return
    if text == "今月の給与確認":
        salary_handlers.handle_salary(event, user_id, {})
        return
    if text == "先月の給与確認":
        now = datetime.now(config.TIMEZONE)
        lm = f"{now.year - 1}/12" if now.month == 1 else f"{now.year}/{now.month - 1:02d}"
        salary_handlers.handle_salary(event, user_id, {"year_month": lm})
        return
    if text == "手当一覧":
        payslip_handlers._handle_list_allowances(event, user_id)
        return
    if text == "控除一覧":
        payslip_handlers._handle_list_custom_deductions(event, user_id)
        return
    if text == "有給残日数を確認":
        leave_handlers._handle_check_leave(event, user_id)
        return
    if text == "有給付与履歴":
        leave_handlers._handle_leave_history(event, user_id)
        return
    if text in ("仕事名一覧", "プロファイル一覧"):
        profile_handlers._handle_list_profiles(event, user_id)
        return
    if text == "Googleカレンダーを連携する":
        settings_handlers._handle_connect_calendar(event, user_id)
        return

    # 名前登録の入力待ち：自然言語から名前部分だけを抽出して登録
    if user_id in state.name_input_mode:
        state.name_input_mode.discard(user_id)
        name = settings_handlers._extract_employee_name(text)
        if name:
            user_settings.update_setting(user_id, "employee_name", name)
            line_service.reply_with_quickreply(reply_token,
                f"✅ シフト表での名前を「{name}」に登録しました。\n"
                f"次回からシフト表の写真を送ると「{name}」の行を自動で抽出します。",
                settings_handlers._settings_qr()
            )
        else:
            line_service.reply_text(reply_token, "名前が入力されていません。もう一度「仕事名」→「名前を設定」から登録してください。")
        return

    # 設定変更の入力待ち：値を受け取って設定を更新する
    if user_id in state.setting_input_mode:
        setting_type = state.setting_input_mode.pop(user_id)
        value = settings_handlers._parse_setting_input(setting_type, text)
        if value is None:
            line_service.reply_text(reply_token, f"入力を認識できませんでした。\n{state.SETTING_PROMPTS[setting_type][1]}\nのように入力してください。")
            return
        if setting_type == "notify_time":
            user_settings.update_setting(user_id, "notify_enabled", "ON")
            user_settings.update_setting(user_id, "notify_time", value)
            line_service.reply_with_quickreply(reply_token,
                f"✅ 通知時刻を {value} に設定し、通知をONにしました。",
                settings_handlers._settings_qr()
            )
        else:
            user_settings.update_setting(user_id, setting_type, value)
            label = state.SETTING_LABELS.get(setting_type, setting_type)
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
                    settings_handlers._settings_qr()
                )
        return

    # 有給付与日数の入力待ち：数字だけ受け取って即登録する
    if user_id in state.leave_grant_input_mode:
        state.leave_grant_input_mode.discard(user_id)
        import re
        m = re.search(r'\d+\.?\d*', text)
        if not m:
            line_service.reply_text(reply_token, "日数を数字で送ってください。\n例：10")
            return
        leave_handlers._handle_grant_leave(event, user_id, {"days": float(m.group())})
        return

    # ヘルプキーワード検索モード：次の1メッセージを検索クエリとして処理
    if user_id in state.help_mode:
        state.help_mode.discard(user_id)
        settings_handlers._handle_help_search(event, user_id, text)
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
        shift_handlers._handle_register(event, parsed, user_id)
    elif intent == "REGISTER_MULTIPLE_SHIFTS":
        shift_handlers._handle_register_multiple(event, parsed, user_id)
    elif intent == "REGISTER_SHIFTS_BATCH":
        shift_handlers._handle_register_batch(event, parsed, user_id)
    elif intent == "DELETE_SHIFT":
        shift_handlers._handle_delete(event, parsed, user_id)
    elif intent == "UPDATE_SHIFT":
        shift_handlers._handle_update_shift(event, parsed, user_id)
    elif intent == "LIST_SHIFTS":
        shift_handlers._handle_list(event, user_id, parsed)
    elif intent == "MONTHLY_SALARY":
        salary_handlers.handle_salary(event, user_id, parsed)
    elif intent == "UPDATE_SETTING":
        settings_handlers._handle_update_setting(event, user_id, parsed)
    elif intent == "CHECK_SETTING":
        line_service.reply_with_quickreply(
            reply_token,
            user_settings.format_settings(user_settings.get_settings(user_id)),
            settings_handlers._settings_qr()
        )
    elif intent == "GRANT_LEAVE":
        leave_handlers._handle_grant_leave(event, user_id, parsed)
    elif intent == "USE_LEAVE":
        leave_handlers._handle_use_leave(event, user_id, parsed)
    elif intent == "CHECK_LEAVE":
        leave_handlers._handle_check_leave(event, user_id)
    elif intent == "DELETE_LEAVE":
        leave_handlers._handle_delete_leave(event, user_id, parsed)
    elif intent == "MODIFY_LEAVE":
        leave_handlers._handle_modify_leave(event, user_id, parsed)
    elif intent == "CREATE_PROFILE":
        profile_handlers._handle_create_profile(event, user_id, parsed)
    elif intent == "SWITCH_PROFILE":
        profile_handlers._handle_switch_profile(event, user_id, parsed)
    elif intent == "UPDATE_PROFILE":
        profile_handlers._handle_update_profile(event, user_id, parsed)
    elif intent == "LIST_PROFILES":
        profile_handlers._handle_list_profiles(event, user_id)
    elif intent == "DELETE_PROFILE":
        profile_handlers._handle_delete_profile(event, user_id, parsed)
    elif intent == "REGISTER_DEDUCTIONS":
        payslip_handlers._handle_register_deductions(event, user_id, parsed)
    elif intent == "DELETE_DEDUCTIONS":
        payslip_handlers._handle_delete_deduction(event, user_id, parsed)
    elif intent == "MODIFY_DEDUCTIONS":
        payslip_handlers._handle_modify_deduction(event, user_id, parsed)
    elif intent == "CREATE_ALLOWANCE":
        payslip_handlers._handle_create_allowance(event, user_id, parsed)
    elif intent == "LIST_ALLOWANCES":
        payslip_handlers._handle_list_allowances(event, user_id)
    elif intent == "DELETE_ALLOWANCE":
        payslip_handlers._handle_delete_allowance(event, user_id, parsed)
    elif intent == "CREATE_CUSTOM_DEDUCTION":
        payslip_handlers._handle_create_custom_deduction(event, user_id, parsed)
    elif intent == "LIST_CUSTOM_DEDUCTIONS":
        payslip_handlers._handle_list_custom_deductions(event, user_id)
    elif intent == "DELETE_CUSTOM_DEDUCTION":
        payslip_handlers._handle_delete_custom_deduction(event, user_id, parsed)
    elif intent == "DELETE_ALL_DATA":
        salary_handlers.handle_delete_all(event, user_id)
    elif intent == "CONNECT_CALENDAR":
        settings_handlers._handle_connect_calendar(event, user_id)
    elif intent == "DISCONNECT_CALENDAR":
        settings_handlers._handle_disconnect_calendar(event, user_id)
    elif intent == "HELP":
        settings_handlers._handle_help(event, user_id)
    else:
        line_service.reply_text(reply_token,
            "うまく解釈できませんでした。\n"
            "「ヘルプ」と送るとコマンド一覧を確認できます。\n"
            "「メニュー」と送ると操作の選択肢を表示します。"
        )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id
    shift_handlers._handle_image_shift(event, user_id)


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
        state.help_mode.discard(user_id)

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
        shift_handlers.confirm_register(user_id, reply_token, data)
    elif action == "confirm_register_multi":
        shift_handlers.confirm_register_multi(user_id, reply_token)
    elif action == "confirm_delete":
        shift_handlers.confirm_delete(user_id, reply_token, data)
    elif action == "confirm_deductions":
        payslip_handlers.confirm_deductions(user_id, reply_token)
    elif action == "delete_all_stage1":
        salary_handlers.confirm_delete_all_stage1(reply_token)
    elif action == "delete_all_stage2":
        salary_handlers.confirm_delete_all_stage2(user_id, reply_token)
    elif action == "confirm_disconnect_calendar":
        settings_handlers.confirm_disconnect_calendar(user_id, reply_token)
    elif action == "confirm_delete_deductions":
        payslip_handlers.confirm_delete_deductions(user_id, reply_token)
    elif action == "confirm_modify_deductions":
        payslip_handlers.confirm_modify_deductions(user_id, reply_token, data)
    elif action == "select_delete_allowance":
        payslip_handlers.select_delete_allowance(user_id, reply_token, data)
    elif action == "confirm_delete_allowance":
        payslip_handlers.confirm_delete_allowance(user_id, reply_token)
    elif action == "select_delete_custom_deduction":
        payslip_handlers.select_delete_custom_deduction(user_id, reply_token, data)
    elif action == "confirm_delete_custom_deduction":
        payslip_handlers.confirm_delete_custom_deduction(user_id, reply_token)
    elif action == "select_delete_profile":
        profile_handlers.select_delete_profile(user_id, reply_token, data)
    elif action == "delete_profile_stage1":
        profile_handlers.delete_profile_stage1(reply_token, data)
    elif action == "confirm_delete_profile":
        profile_handlers.confirm_delete_profile(user_id, reply_token, data)
    elif action == "switch_profile_direct":
        profile_handlers.switch_profile_direct(user_id, reply_token, data)
    elif action == "confirm_use_leave":
        leave_handlers.confirm_use_leave(user_id, reply_token)
    elif action == "confirm_update_shift":
        shift_handlers.confirm_update_shift(user_id, reply_token)
    elif action == "confirm_delete_leave":
        leave_handlers.confirm_delete_leave(user_id, reply_token)
    elif action == "confirm_modify_leave":
        leave_handlers.confirm_modify_leave(user_id, reply_token)
    elif action == "help_category":
        settings_handlers.help_category(user_id, reply_token, data)
    elif action == "help_item":
        settings_handlers.help_item(reply_token, data, user_id)
    elif action == "help_show_all":
        settings_handlers.help_show_all(reply_token)
    elif action == "help_search_again":
        settings_handlers.help_search_again(user_id, reply_token)
    elif action == "cancel":
        line_service.reply_text(reply_token, "キャンセルしました。")
    elif action == "skip_allowance":
        payslip_handlers.skip_allowance(user_id, reply_token)
    elif action == "select_shift_name":
        shift_handlers.select_shift_name(user_id, reply_token, data)
    elif action == "select_payslip_allowance":
        payslip_handlers.select_payslip_allowance(user_id, reply_token, data)
    elif action == "register_payslip_allowance":
        payslip_handlers.register_payslip_allowance(user_id, reply_token, data)
    elif action == "select_payslip_deduction":
        payslip_handlers.select_payslip_deduction(user_id, reply_token, data)
    elif action == "register_payslip_deduction":
        payslip_handlers.register_payslip_deduction(user_id, reply_token, data)
    elif action == "skip_payslip_deductions":
        payslip_handlers.skip_payslip_deductions(user_id, reply_token)
    elif action == "close_menu":
        pass  # クイックリプライを閉じるだけ。返信なし。
    elif action == "connect_calendar":
        settings_handlers._handle_connect_calendar_postback(reply_token, user_id)
    elif action == "disconnect_calendar":
        settings_handlers._handle_disconnect_calendar_postback(reply_token, user_id)
    # ── 設定変更アクション ────────────────────────────────
    elif action == "setting_input_start":
        settings_handlers.setting_input_start(user_id, reply_token, data)
    elif action == "setting_notify_menu":
        settings_handlers.setting_notify_menu(user_id, reply_token)
    elif action == "setting_calendar_menu":
        settings_handlers.setting_calendar_menu(user_id, reply_token)
    elif action == "setting_color_menu":
        settings_handlers.setting_color_menu(reply_token)
    elif action == "setting_social_menu":
        settings_handlers.setting_social_menu(user_id, reply_token)
    elif action == "setting_set_value":
        settings_handlers.setting_set_value(user_id, reply_token, data)
    elif action == "update_past_shifts_color":
        settings_handlers.update_past_shifts_color(user_id, reply_token, data)
    elif action == "setting_show":
        settings_handlers.setting_show(user_id, reply_token)
    elif action == "menu_help":
        settings_handlers._handle_help(event, user_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
