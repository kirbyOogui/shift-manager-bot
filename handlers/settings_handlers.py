"""ユーザー設定変更、Googleカレンダー連携/解除、ヘルプ検索を扱うハンドラー群。"""

import json
import logging
import re

import calendar_service
import config
import help_content
import line_service
import sheets_service
import state
import user_settings
from handlers import salary_handlers

logger = logging.getLogger(__name__)


def _extract_employee_name(text: str) -> str:
    """自然な言い回しから従業員名だけを抽出する。"""
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


def _send_oauth_prompt(reply_token: str, user_id: str) -> None:
    """Googleカレンダー連携URLを案内する。"""
    start_url = f"{config.APP_BASE_URL}/oauth/start?user_id={user_id}"
    line_service.reply_text(reply_token,
        f"以下のURLをタップしてGoogleカレンダーと連携できます。\n"
        f"（URLの有効期限は10分です）\n\n"
        f"{start_url}"
    )


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


def _handle_update_setting(event, user_id: str, parsed: dict):
    setting_type = parsed.get("setting_type", "")
    value = parsed.get("value")

    if setting_type == "employee_name" and not value:
        state.name_input_mode.add(user_id)
        line_service.reply_text(event.reply_token,
            "📝 シフト表に表示されているあなたの名前（フルネーム）を送ってください。\n\n"
            "例：山田太郎"
        )
        return

    if user_settings.update_setting(user_id, setting_type, value):
        if setting_type == "calendar_color":
            state.pending_color_update[user_id] = value
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


def _handle_help(event, user_id: str) -> None:
    """ヘルプキーワード検索モードを開始する。"""
    state.help_mode.add(user_id)
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
        state.help_mode.add(user_id)
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


# ── ポストバック（ボタン確定後の実処理） ─────────────────────

def confirm_disconnect_calendar(user_id: str, reply_token: str) -> None:
    if sheets_service.clear_google_tokens(user_id):
        line_service.reply_text(reply_token,
            "🔓 Googleカレンダーとの連携を解除しました。\n"
            "シフトデータはスプレッドシートに引き続き保存されます。\n"
            "再連携するには「Googleカレンダーを連携する」と送ってください。")
    else:
        line_service.reply_text(reply_token, "連携解除に失敗しました。")


def setting_input_start(user_id: str, reply_token: str, data: dict) -> None:
    setting = data.get("setting", "")
    if setting == "employee_name":
        state.name_input_mode.add(user_id)
        line_service.reply_text(reply_token,
            "📝 シフト表に表示されているあなたの名前（フルネーム）を送ってください。\n\n例：山田太郎"
        )
    elif setting in state.SETTING_PROMPTS:
        state.setting_input_mode[user_id] = setting
        prompt, example = state.SETTING_PROMPTS[setting]
        line_service.reply_text(reply_token, f"{prompt}\n\n{example}")
    else:
        line_service.reply_text(reply_token, "不明な設定項目です。")


def setting_notify_menu(user_id: str, reply_token: str) -> None:
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


def setting_calendar_menu(user_id: str, reply_token: str) -> None:
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


def setting_color_menu(reply_token: str) -> None:
    qr = [
        {"label": color, "data": json.dumps({"action": "setting_set_value", "setting": "calendar_color", "value": color})}
        for color in state.CALENDAR_COLORS
    ]
    qr.append({"label": "◀ カレンダー設定", "data": json.dumps({"action": "setting_calendar_menu"})})
    line_service.reply_with_quickreply(reply_token,
        "🎨 カレンダーのカラーを選んでください。",
        qr
    )


def setting_social_menu(user_id: str, reply_token: str) -> None:
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


def setting_set_value(user_id: str, reply_token: str, data: dict) -> None:
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
            state.pending_color_update[user_id] = value
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


def update_past_shifts_color(user_id: str, reply_token: str, data: dict) -> None:
    apply      = data.get("apply", False)
    color_name = state.pending_color_update.pop(user_id, None)
    if not apply or not color_name:
        line_service.reply_with_quickreply(reply_token,
            "今後登録するシフトから新しい色が適用されます。",
            [{"label": "📋 設定一覧", "data": json.dumps({"action": "setting_show"})}]
        )
        return
    color_id = salary_handlers.resolve_color(color_name)
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


def setting_show(user_id: str, reply_token: str) -> None:
    line_service.reply_with_quickreply(
        reply_token,
        user_settings.format_settings(user_settings.get_settings(user_id)),
        _settings_qr()
    )


def help_category(user_id: str, reply_token: str, data: dict) -> None:
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


def help_item(reply_token: str, data: dict, user_id: str) -> None:
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


def help_show_all(reply_token: str) -> None:
    cats = help_content.get_category_list()
    qr = [{"label": c["label"], "data": json.dumps({"action": "help_category", "cat": c["cat"]})} for c in cats]
    qr.append({"label": "🔍 キーワード検索", "data": json.dumps({"action": "help_search_again"})})
    line_service.reply_with_quickreply(reply_token, help_content.get_all_text(), qr)


def help_search_again(user_id: str, reply_token: str) -> None:
    state.help_mode.add(user_id)
    line_service.reply_text(reply_token,
        "🔍 調べたいキーワードを送ってください。\n"
        "（例：シフト、有給、給与、仕事名...）\n\n"
        "「一覧」と送ると全機能を表示します。"
    )
