import sheets_service

_FIELD_MAP = {
    "hourly_wage": "時給(円)",
    "break_minutes": "休憩時間(分)",
    "notify_time": "通知時刻",
    "notify_enabled": "通知ON/OFF",
    "calendar_title": "カレンダータイトル",
    "calendar_color": "カレンダーカラー",
    "active_profile": "アクティブプロファイル",
    "social_insurance": "社会保険加入",
    "night_rate":  "深夜割増率",
    "early_rate":  "早朝割増率",
    "early_end":   "早朝終了時刻",
    "employee_name": "従業員名",
    "ocr_name": "OCR名",
    "leave_hours": "有給標準時間(時間)",
}


def get_settings(user_id: str, display_name: str = "") -> dict:
    return sheets_service.get_or_create_user(user_id, display_name)


def update_setting(user_id: str, setting_type: str, value) -> bool:
    field = _FIELD_MAP.get(setting_type)
    if not field:
        return False
    return sheets_service.update_user_setting(user_id, field, value)


def format_settings(settings: dict) -> str:
    color = settings.get("カレンダーカラー", "") or "未設定"
    active = settings.get("アクティブプロファイル", "") or "なし（グローバル設定を使用）"
    employee_name = settings.get("従業員名", "") or "未設定"
    leave_hours_raw = settings.get("有給標準時間(時間)", "") or ""
    leave_hours = f"{leave_hours_raw}時間" if leave_hours_raw else "未設定"
    return (
        f"⚙️ 現在の設定\n"
        f"選択中の仕事名：{active}\n"
        f"時給：{settings.get('時給(円)', '-')}円\n"
        f"休憩時間：{settings.get('休憩時間(分)', '-')}分\n"
        f"通知時刻：{settings.get('通知時刻', '-')}\n"
        f"通知：{settings.get('通知ON/OFF', '-')}\n"
        f"カレンダー予定名：{settings.get('カレンダータイトル', 'シフト')}\n"
        f"カレンダーカラー：{color}\n"
        f"社会保険加入：{settings.get('社会保険加入', 'なし') or 'なし'}\n"
        f"深夜割増率：{settings.get('深夜割増率', 25)}%（22:00〜5:00）\n"
        f"早朝割増率：{settings.get('早朝割増率', 0)}%（5:00〜{settings.get('早朝終了時刻', '08:00')}）\n"
        f"シフト表の名前：{employee_name}\n"
        f"有給標準時間：{leave_hours}"
    )
