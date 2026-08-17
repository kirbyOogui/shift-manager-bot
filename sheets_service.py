import json
import time
from datetime import datetime
from google.oauth2.service_account import Credentials
import gspread
import config

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_SETTINGS_HEADERS = [
    "LINE UserID", "表示名", "時給(円)", "休憩時間(分)",
    "通知時刻", "通知ON/OFF", "カレンダータイトル", "カレンダーカラー", "アクティブプロファイル", "初回登録日時",
    "access_token", "refresh_token", "token_expiry",
]

_ALLOWANCE_HEADERS = [
    "LINE UserID", "手当名", "タイプ", "金額", "割増率(%)",
    "期間開始月", "期間開始日", "期間終了月", "期間終了日",
    "プロファイル名", "有効", "登録日時",
]

_CUSTOM_DEDUCTION_HEADERS = [
    "LINE UserID", "控除名", "タイプ", "金額", "率(%)",
    "プロファイル名", "有効", "登録日時",
]

_DEDUCTION_HEADERS = [
    "LINE UserID", "年月", "プロファイル名", "総支給額",
    "健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他",
    "変動控除(JSON)",
    "登録日時",
]

_PROFILE_HEADERS = [
    "LINE UserID", "プロファイル名", "カレンダータイトル",
    "デフォルト開始時刻", "デフォルト終了時刻", "休憩時間(分)", "カレンダーカラー", "時給(円)",
    "締め日", "給料日", "有給標準時間(時間)",
]

_LEAVE_HEADERS = [
    "LINE UserID", "日付", "種類", "種別", "日数", "プロファイル名", "有効期限", "備考", "登録日時",
]

_SHIFT_HEADERS = [
    "LINE UserID", "日付", "開始時刻", "終了時刻", "実働時間(分)", "Calendar EventID", "登録日時", "プロファイル名",
]

# ── キャッシュ ─────────────────────────────────────────
_ss: gspread.Spreadsheet | None = None   # Spreadsheetオブジェクトを使い回す
_cache: dict = {}                        # {シート名: {"data": list, "ts": float}}
_CACHE_TTL = 30                          # キャッシュ有効秒数


def _ensure_headers(ws, required_headers: list) -> list:
    """ワークシートに不足しているヘッダー列を右端へ追加し、最新ヘッダーリストを返す。"""
    import logging as _logging
    headers = ws.row_values(1)
    missing = [h for h in required_headers if h not in headers]
    for col_name in missing:
        headers.append(col_name)
        col_idx = len(headers)
        if col_idx > ws.col_count:
            ws.resize(cols=col_idx)
        ws.update_cell(1, col_idx, col_name)
        _logging.getLogger(__name__).info(f"[sheets] ヘッダー列を追加: {col_name!r}")
    return headers


def _get_spreadsheet() -> gspread.Spreadsheet:
    """Spreadsheetオブジェクトをキャッシュして返す。接続エラー時は再接続する。"""
    global _ss
    if _ss is None:
        creds_info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=_SCOPES)
        _ss = gspread.authorize(creds).open_by_key(config.GOOGLE_SPREADSHEET_ID)
    return _ss


def _reset_ss() -> None:
    """接続エラー時にSpreadsheetオブジェクトをリセットする。"""
    global _ss
    _ss = None


def _get_records(ws) -> list:
    """get_all_records()の結果を_CACHE_TTL秒間キャッシュして返す。"""
    key = ws.title
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < _CACHE_TTL:
        return _cache[key]["data"]
    data = ws.get_all_records()
    _cache[key] = {"data": data, "ts": now}
    return data


def _invalidate(sheet_name: str) -> None:
    """書き込み後に指定シートのキャッシュを削除する。"""
    _cache.pop(sheet_name, None)


def ensure_sheets() -> None:
    """必要なシートが存在しない場合に作成する。既存シートにトークン列がない場合は追加する。"""
    ss = _get_spreadsheet()
    existing = [ws.title for ws in ss.worksheets()]

    if config.SHEET_ALLOWANCES not in existing:
        ws = ss.add_worksheet(title=config.SHEET_ALLOWANCES, rows=200, cols=len(_ALLOWANCE_HEADERS))
        ws.append_row(_ALLOWANCE_HEADERS)

    if config.SHEET_CUSTOM_DEDUCTIONS not in existing:
        ws = ss.add_worksheet(title=config.SHEET_CUSTOM_DEDUCTIONS, rows=200, cols=len(_CUSTOM_DEDUCTION_HEADERS))
        ws.append_row(_CUSTOM_DEDUCTION_HEADERS)

    if config.SHEET_DEDUCTIONS not in existing:
        ws = ss.add_worksheet(title=config.SHEET_DEDUCTIONS, rows=500, cols=len(_DEDUCTION_HEADERS))
        ws.append_row(_DEDUCTION_HEADERS)
    else:
        ws = ss.worksheet(config.SHEET_DEDUCTIONS)
        headers = ws.row_values(1)
        if "プロファイル名" not in headers:
            col_idx = len(headers) + 1
            if col_idx > ws.col_count:
                ws.resize(cols=col_idx + 2)
            ws.update_cell(1, 3, "プロファイル名")

    if config.SHEET_PROFILES not in existing:
        ws = ss.add_worksheet(title=config.SHEET_PROFILES, rows=500, cols=len(_PROFILE_HEADERS))
        ws.append_row(_PROFILE_HEADERS)
    else:
        ws = ss.worksheet(config.SHEET_PROFILES)
        headers = ws.row_values(1)
        for field in ["締め日", "給料日", "有給標準時間(時間)"]:
            if field not in headers:
                headers.append(field)
                col_idx = len(headers)
                if col_idx > ws.col_count:
                    ws.resize(cols=col_idx + 2)
                ws.update_cell(1, col_idx, field)

    if config.SHEET_LEAVE not in existing:
        ws = ss.add_worksheet(title=config.SHEET_LEAVE, rows=500, cols=len(_LEAVE_HEADERS))
        ws.append_row(_LEAVE_HEADERS)
    else:
        ws = ss.worksheet(config.SHEET_LEAVE)
        headers = ws.row_values(1)
        if "プロファイル名" not in headers:
            col_idx = len(headers) + 1
            if col_idx > ws.col_count:
                ws.resize(cols=col_idx + 2)
            ws.update_cell(1, col_idx, "プロファイル名")

    if config.SHEET_SHIFTS not in existing:
        ws = ss.add_worksheet(title=config.SHEET_SHIFTS, rows=1000, cols=len(_SHIFT_HEADERS))
        ws.append_row(_SHIFT_HEADERS)
    else:
        _ensure_headers(ss.worksheet(config.SHEET_SHIFTS), _SHIFT_HEADERS)

    if config.SHEET_SETTINGS not in existing:
        ws = ss.add_worksheet(title=config.SHEET_SETTINGS, rows=100, cols=len(_SETTINGS_HEADERS))
        ws.append_row(_SETTINGS_HEADERS)
    else:
        ws = ss.worksheet(config.SHEET_SETTINGS)
        headers = ws.row_values(1)
        for field in ["カレンダータイトル", "カレンダーカラー", "アクティブプロファイル", "社会保険加入",
                      "深夜割増率", "早朝割増率", "早朝終了時刻", "従業員名", "有給標準時間(時間)",
                      "access_token", "refresh_token", "token_expiry"]:
            if field not in headers:
                headers.append(field)
                col_idx = len(headers)
                if col_idx > ws.col_count:
                    ws.resize(cols=col_idx + 4)
                ws.update_cell(1, col_idx, field)

    # 初期化後はキャッシュをクリアしてスキーマ変更を反映
    _cache.clear()


def get_or_create_user(user_id: str, display_name: str = "") -> dict:
    """ユーザー設定を取得する。存在しない場合はデフォルト値で新規登録する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    records = _get_records(ws)

    for row in records:
        if row.get("LINE UserID") == user_id:
            return row

    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")
    defaults = {
        "LINE UserID": user_id, "表示名": display_name,
        "時給(円)": config.DEFAULT_HOURLY_WAGE, "休憩時間(分)": config.DEFAULT_BREAK_MINUTES,
        "通知時刻": config.DEFAULT_NOTIFY_TIME, "通知ON/OFF": config.DEFAULT_NOTIFY_ENABLED,
        "カレンダータイトル": config.DEFAULT_CALENDAR_TITLE,
        "カレンダーカラー": config.DEFAULT_CALENDAR_COLOR,
        "アクティブプロファイル": "", "社会保険加入": "なし",
        "深夜割増率": config.DEFAULT_NIGHT_RATE,
        "早朝割増率": config.DEFAULT_EARLY_RATE,
        "早朝終了時刻": config.DEFAULT_EARLY_END,
        "初回登録日時": now,
        "access_token": "", "refresh_token": "", "token_expiry": "",
    }
    headers = ws.row_values(1)
    new_row = [defaults.get(h, "") for h in headers]
    ws.append_row(new_row)
    _invalidate(config.SHEET_SETTINGS)
    return defaults


def update_user_setting(user_id: str, field: str, value) -> bool:
    """指定フィールドの設定値を更新する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    records = _get_records(ws)
    headers = ws.row_values(1)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id:
            if field not in headers:
                return False
            ws.update_cell(i + 2, headers.index(field) + 1, value)
            _invalidate(config.SHEET_SETTINGS)
            return True
    return False


def save_user_tokens(user_id: str, access_token: str, refresh_token: str, token_expiry: str) -> None:
    """ユーザーのOAuthトークンを保存する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    records = _get_records(ws)
    headers = ws.row_values(1)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id:
            for field, value in [
                ("access_token", access_token),
                ("refresh_token", refresh_token),
                ("token_expiry", token_expiry),
            ]:
                if field in headers:
                    ws.update_cell(i + 2, headers.index(field) + 1, value)
            _invalidate(config.SHEET_SETTINGS)
            return


def get_user_tokens(user_id: str) -> dict | None:
    """ユーザーのOAuthトークンを返す。存在しない場合はNoneを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    records = _get_records(ws)

    for row in records:
        if row.get("LINE UserID") == user_id:
            return {
                "access_token": row.get("access_token", ""),
                "refresh_token": row.get("refresh_token", ""),
                "token_expiry": row.get("token_expiry", ""),
            }
    return None


def has_google_token(user_id: str) -> bool:
    """ユーザーがGoogleカレンダーと連携済みかどうかを返す。"""
    tokens = get_user_tokens(user_id)
    return bool(tokens and tokens.get("refresh_token"))


def delete_all_user_data(user_id: str) -> None:
    """ユーザーの全データをスプレッドシートから削除する。
    Googleカレンダー上の予定は削除しない（トークンのみ解除）。"""
    ss = _get_spreadsheet()

    ws_shifts = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws_shifts)
    rows_to_delete = [i + 2 for i, r in enumerate(records) if r.get("LINE UserID") == user_id]
    for row_idx in sorted(rows_to_delete, reverse=True):
        ws_shifts.delete_rows(row_idx)
    _invalidate(config.SHEET_SHIFTS)

    def _delete_user_rows(sheet_name: str) -> None:
        ws = ss.worksheet(sheet_name)
        recs = _get_records(ws)
        idxs = [i + 2 for i, r in enumerate(recs) if r.get("LINE UserID") == user_id]
        for idx in sorted(idxs, reverse=True):
            ws.delete_rows(idx)
        _invalidate(sheet_name)

    _delete_user_rows(config.SHEET_DEDUCTIONS)
    _delete_user_rows(config.SHEET_LEAVE)
    _delete_user_rows(config.SHEET_PROFILES)
    _delete_user_rows(config.SHEET_ALLOWANCES)
    _delete_user_rows(config.SHEET_CUSTOM_DEDUCTIONS)

    ws_set = ss.worksheet(config.SHEET_SETTINGS)
    set_records = _get_records(ws_set)
    headers = ws_set.row_values(1)
    keep_cols = {"LINE UserID", "表示名"}
    for i, row in enumerate(set_records):
        if row.get("LINE UserID") == user_id:
            for col_idx, h in enumerate(headers, start=1):
                if h not in keep_cols:
                    ws_set.update_cell(i + 2, col_idx, "")
            break
    _invalidate(config.SHEET_SETTINGS)


def clear_google_tokens(user_id: str) -> bool:
    """ユーザーのGoogleOAuthトークンを削除（連携解除）する。成功でTrueを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    records = _get_records(ws)
    headers = ws.row_values(1)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id:
            for field in ["access_token", "refresh_token", "token_expiry"]:
                if field in headers:
                    ws.update_cell(i + 2, headers.index(field) + 1, "")
            _invalidate(config.SHEET_SETTINGS)
            return True
    return False


def get_all_users() -> list:
    """全ユーザーの設定を返す（通知チェック用）。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SETTINGS)
    return _get_records(ws)


def save_shift(user_id: str, date_str: str, start_time: str, end_time: str, work_minutes: int, event_id: str,
               profile_name: str = "") -> None:
    """シフトデータを保存する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    headers = _ensure_headers(ws, _SHIFT_HEADERS)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")
    values = {
        "LINE UserID": user_id, "日付": date_str, "開始時刻": start_time, "終了時刻": end_time,
        "実働時間(分)": work_minutes, "Calendar EventID": event_id, "登録日時": now, "プロファイル名": profile_name,
    }
    row = [""] * len(headers)
    for k, v in values.items():
        if k in headers:
            row[headers.index(k)] = v
    ws.append_row(row)
    _invalidate(config.SHEET_SHIFTS)


def delete_shift(user_id: str, date_str: str):
    """指定日のシフトを削除する。見つかった場合はCalendar EventIDを返し、見つからない場合はNoneを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("日付") == date_str:
            event_id = row.get("Calendar EventID", "")
            ws.delete_rows(i + 2)
            _invalidate(config.SHEET_SHIFTS)
            return event_id
    return None


def get_monthly_shifts(user_id: str, year: int, month: int) -> list:
    """指定月のシフト一覧を日付順で返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws)

    result = []
    for row in records:
        if row.get("LINE UserID") != user_id:
            continue
        try:
            d = datetime.strptime(row["日付"], "%Y/%m/%d")
            if d.year == year and d.month == month:
                result.append(row)
        except (ValueError, KeyError):
            continue

    return sorted(result, key=lambda x: x["日付"])


def get_shifts_in_period(user_id: str, start_str: str, end_str: str, profile_name: str = None) -> list:
    """指定期間（YYYY/MM/DD）のシフト一覧を日付順で返す。
    profile_name指定時は、そのプロファイルで登録されたシフト＋プロファイル未設定（旧データ）のシフトのみ返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = [r for r in _get_records(ws)
               if r.get("LINE UserID") == user_id
               and start_str <= r.get("日付", "") <= end_str]
    if profile_name is not None:
        records = [r for r in records if r.get("プロファイル名", "") in (profile_name, "")]
    return sorted(records, key=lambda x: x["日付"])


def get_all_shifts_with_event_id(user_id: str) -> list:
    """Calendar EventID を持つユーザーの全シフトを日付順で返す（色一括更新用）。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws)
    result = [
        r for r in records
        if r.get("LINE UserID") == user_id
        and r.get("Calendar EventID", "").strip()
    ]
    return sorted(result, key=lambda x: x.get("日付", ""))


def update_shift(user_id: str, date_str: str, new_start: str, new_end: str, new_work_min: int) -> str | None:
    """指定日のシフトを更新する。成功時はCalendar EventIDを返す。見つからない場合はNoneを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws)
    headers = ws.row_values(1)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("日付") == date_str:
            ws.update_cell(i + 2, headers.index("開始時刻") + 1, new_start)
            ws.update_cell(i + 2, headers.index("終了時刻") + 1, new_end)
            ws.update_cell(i + 2, headers.index("実働時間(分)") + 1, new_work_min)
            _invalidate(config.SHEET_SHIFTS)
            return row.get("Calendar EventID", "")
    return None


def get_shift_by_date(user_id: str, date_str: str) -> dict | None:
    """指定日のシフトを返す。存在しない場合はNoneを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_SHIFTS)
    records = _get_records(ws)

    for row in records:
        if row.get("LINE UserID") == user_id and row.get("日付") == date_str:
            return row
    return None


# ── プロファイル操作 ─────────────────────────────────

def get_profiles(user_id: str) -> list:
    """ユーザーの全プロファイルを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_PROFILES)
    return [r for r in _get_records(ws) if r.get("LINE UserID") == user_id]


def get_profile(user_id: str, name: str) -> dict | None:
    """指定名のプロファイルを返す。存在しない場合はNoneを返す。"""
    for p in get_profiles(user_id):
        if p.get("プロファイル名") == name:
            return p
    return None


def upsert_profile(user_id: str, name: str, fields: dict) -> None:
    """プロファイルを作成または更新する。fieldsで指定したフィールドのみ書き込む。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_PROFILES)
    records = _get_records(ws)
    headers = ws.row_values(1)

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("プロファイル名") == name:
            for field, value in fields.items():
                if field in headers:
                    ws.update_cell(i + 2, headers.index(field) + 1, value)
            _invalidate(config.SHEET_PROFILES)
            return

    new_row = [""] * len(headers)
    new_row[headers.index("LINE UserID")] = user_id
    new_row[headers.index("プロファイル名")] = name
    for field, value in fields.items():
        if field in headers:
            new_row[headers.index(field)] = value
    ws.append_row(new_row)
    _invalidate(config.SHEET_PROFILES)


def delete_profile(user_id: str, name: str) -> bool:
    """指定名のプロファイルと、それに紐づくシフト・給与明細・カスタム手当・カスタム控除を削除する。
    成功時Trueを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_PROFILES)
    records = _get_records(ws)
    deleted = False
    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("プロファイル名") == name:
            ws.delete_rows(i + 2)
            _invalidate(config.SHEET_PROFILES)
            deleted = True
            break
    if not deleted:
        return False

    def _delete_profile_rows(sheet_name: str) -> None:
        sw = ss.worksheet(sheet_name)
        recs = _get_records(sw)
        idxs = [i + 2 for i, r in enumerate(recs)
                if r.get("LINE UserID") == user_id and r.get("プロファイル名") == name]
        for idx in sorted(idxs, reverse=True):
            sw.delete_rows(idx)
        _invalidate(sheet_name)

    _delete_profile_rows(config.SHEET_DEDUCTIONS)
    _delete_profile_rows(config.SHEET_ALLOWANCES)
    _delete_profile_rows(config.SHEET_CUSTOM_DEDUCTIONS)
    _delete_profile_rows(config.SHEET_SHIFTS)
    return True


# ── 控除データ操作 ─────────────────────────────────────

def save_deduction(user_id: str, year_month: str, gross: int, items: dict,
                   profile_name: str = "", deductions_extra: list = None) -> None:
    """控除データを保存または上書きする。year_monthはYYYY/MM形式。同じ年月・プロファイルは上書き。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_DEDUCTIONS)
    headers = _ensure_headers(ws, _DEDUCTION_HEADERS)
    records = _get_records(ws)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")
    extra_json = json.dumps(deductions_extra or [], ensure_ascii=False)

    fields = ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]
    for i, row in enumerate(records):
        if (row.get("LINE UserID") == user_id
                and row.get("年月") == year_month
                and row.get("プロファイル名", "") == profile_name):
            if "プロファイル名" in headers:
                ws.update_cell(i + 2, headers.index("プロファイル名") + 1, profile_name)
            ws.update_cell(i + 2, headers.index("総支給額") + 1, gross)
            for f in fields:
                if f in headers:
                    ws.update_cell(i + 2, headers.index(f) + 1, items.get(f, 0))
            if "変動控除(JSON)" in headers:
                ws.update_cell(i + 2, headers.index("変動控除(JSON)") + 1, extra_json)
            ws.update_cell(i + 2, headers.index("登録日時") + 1, now)
            _invalidate(config.SHEET_DEDUCTIONS)
            return

    new_row = [""] * len(headers)
    new_row[headers.index("LINE UserID")] = user_id
    new_row[headers.index("年月")] = year_month
    if "プロファイル名" in headers:
        new_row[headers.index("プロファイル名")] = profile_name
    new_row[headers.index("総支給額")] = gross
    for f in fields:
        if f in headers:
            new_row[headers.index(f)] = items.get(f, 0)
    if "変動控除(JSON)" in headers:
        new_row[headers.index("変動控除(JSON)")] = extra_json
    new_row[headers.index("登録日時")] = now
    ws.append_row(new_row)
    _invalidate(config.SHEET_DEDUCTIONS)


def get_deductions(user_id: str, profile_name: str = "") -> list:
    """ユーザーの控除データを年月の新しい順で返す。
    profile_nameが指定されている場合はそのプロファイルのみ。
    該当データがなければ全件にフォールバックする。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_DEDUCTIONS)
    all_records = [r for r in _get_records(ws) if r.get("LINE UserID") == user_id]

    if profile_name:
        filtered = [r for r in all_records if r.get("プロファイル名", "") == profile_name]
        if filtered:
            return sorted(filtered, key=lambda x: x.get("年月", ""), reverse=True)

    return sorted(all_records, key=lambda x: x.get("年月", ""), reverse=True)


def get_deduction_by_month(user_id: str, year_month: str, profile_name: str = "") -> dict | None:
    """指定年月の控除データを返す。存在しない場合はNoneを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_DEDUCTIONS)
    for row in _get_records(ws):
        if (row.get("LINE UserID") == user_id
                and row.get("年月") == year_month
                and row.get("プロファイル名", "") == profile_name):
            return row
    return None


def delete_deduction(user_id: str, year_month: str, profile_name: str = "") -> bool:
    """指定年月の控除データを削除する。削除成功でTrueを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_DEDUCTIONS)
    records = _get_records(ws)
    for i, row in enumerate(records):
        if (row.get("LINE UserID") == user_id
                and row.get("年月") == year_month
                and row.get("プロファイル名", "") == profile_name):
            ws.delete_rows(i + 2)
            _invalidate(config.SHEET_DEDUCTIONS)
            return True
    return False


def update_deduction_field(user_id: str, year_month: str, field: str, value: int,
                           profile_name: str = "") -> bool:
    """指定年月の控除データの特定フィールドのみ更新する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_DEDUCTIONS)
    headers = ws.row_values(1)
    records = _get_records(ws)
    for i, row in enumerate(records):
        if (row.get("LINE UserID") == user_id
                and row.get("年月") == year_month
                and row.get("プロファイル名", "") == profile_name):
            if field not in headers:
                return False
            ws.update_cell(i + 2, headers.index(field) + 1, value)
            _invalidate(config.SHEET_DEDUCTIONS)
            return True
    return False


# ── カスタム手当操作 ───────────────────────────────────

def get_allowances(user_id: str, profile_name: str = "") -> list:
    """ユーザーのカスタム手当を返す。プロファイル指定時はそのプロファイル＋グローバルを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_ALLOWANCES)
    all_rows = [r for r in _get_records(ws) if r.get("LINE UserID") == user_id]
    if profile_name:
        return [r for r in all_rows if r.get("プロファイル名", "") in (profile_name, "")]
    return all_rows


def upsert_allowance(user_id: str, name: str, fields: dict) -> None:
    """手当を作成または上書きする。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_ALLOWANCES)
    records = _get_records(ws)
    headers = ws.row_values(1)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("手当名") == name:
            for field, value in fields.items():
                if field in headers:
                    ws.update_cell(i + 2, headers.index(field) + 1, value)
            ws.update_cell(i + 2, headers.index("登録日時") + 1, now)
            _invalidate(config.SHEET_ALLOWANCES)
            return

    new_row = [""] * len(headers)
    new_row[headers.index("LINE UserID")] = user_id
    new_row[headers.index("手当名")] = name
    new_row[headers.index("有効")] = "yes"
    new_row[headers.index("登録日時")] = now
    for field, value in fields.items():
        if field in headers:
            new_row[headers.index(field)] = value
    ws.append_row(new_row)
    _invalidate(config.SHEET_ALLOWANCES)


def delete_allowance(user_id: str, name: str) -> bool:
    """指定の手当を削除する。成功時Trueを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_ALLOWANCES)
    records = _get_records(ws)
    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("手当名") == name:
            ws.delete_rows(i + 2)
            _invalidate(config.SHEET_ALLOWANCES)
            return True
    return False


# ── カスタム控除操作 ───────────────────────────────────

def get_custom_deductions(user_id: str, profile_name: str = "") -> list:
    """ユーザーのカスタム控除を返す。プロファイル指定時はそのプロファイル＋グローバルを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_CUSTOM_DEDUCTIONS)
    all_rows = [r for r in _get_records(ws) if r.get("LINE UserID") == user_id]
    if profile_name:
        return [r for r in all_rows if r.get("プロファイル名", "") in (profile_name, "")]
    return all_rows


def upsert_custom_deduction(user_id: str, name: str, fields: dict) -> None:
    """控除項目を作成または上書きする。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_CUSTOM_DEDUCTIONS)
    headers = _ensure_headers(ws, _CUSTOM_DEDUCTION_HEADERS)
    records = _get_records(ws)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")

    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("控除名") == name:
            for field, value in fields.items():
                if field in headers:
                    ws.update_cell(i + 2, headers.index(field) + 1, value)
            ws.update_cell(i + 2, headers.index("登録日時") + 1, now)
            _invalidate(config.SHEET_CUSTOM_DEDUCTIONS)
            return

    new_row = [""] * len(headers)
    new_row[headers.index("LINE UserID")] = user_id
    new_row[headers.index("控除名")] = name
    new_row[headers.index("有効")] = "yes"
    new_row[headers.index("登録日時")] = now
    for field, value in fields.items():
        if field in headers:
            new_row[headers.index(field)] = value
    ws.append_row(new_row)
    _invalidate(config.SHEET_CUSTOM_DEDUCTIONS)


def delete_custom_deduction(user_id: str, name: str) -> bool:
    """指定の控除項目を削除する。成功時Trueを返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_CUSTOM_DEDUCTIONS)
    records = _get_records(ws)
    for i, row in enumerate(records):
        if row.get("LINE UserID") == user_id and row.get("控除名") == name:
            ws.delete_rows(i + 2)
            _invalidate(config.SHEET_CUSTOM_DEDUCTIONS)
            return True
    return False


# ── 有給管理 ────────────────────────────────────────────

def grant_leave(user_id: str, leave_type: str, days: float,
                granted_date: str = "", expiry_date: str = "",
                note: str = "", profile_name: str = "") -> None:
    """有給付与を記録する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_LEAVE)
    headers = ws.row_values(1)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")
    date_val = granted_date or now[:10]
    row_data = {
        "LINE UserID": user_id, "日付": date_val, "種類": leave_type,
        "種別": "付与", "日数": days, "プロファイル名": profile_name,
        "有効期限": expiry_date, "備考": note, "登録日時": now,
    }
    ws.append_row([row_data.get(h, "") for h in headers])
    _invalidate(config.SHEET_LEAVE)


def use_leave(user_id: str, date_str: str, leave_type: str, days: float,
              profile_name: str = "") -> None:
    """有給使用を記録する。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_LEAVE)
    headers = ws.row_values(1)
    now = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d %H:%M")
    row_data = {
        "LINE UserID": user_id, "日付": date_str, "種類": leave_type,
        "種別": "使用", "日数": days, "プロファイル名": profile_name,
        "有効期限": "", "備考": "", "登録日時": now,
    }
    ws.append_row([row_data.get(h, "") for h in headers])
    _invalidate(config.SHEET_LEAVE)


def delete_leave_usage(user_id: str, date_str: str, profile_name: str = "") -> dict | None:
    """指定日の有給使用記録を（重複していれば全件）削除する。最初に見つかった行の情報を返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_LEAVE)
    records = _get_records(ws)
    matches = [
        (i, row) for i, row in enumerate(records)
        if row.get("LINE UserID") == user_id
        and row.get("日付") == date_str
        and row.get("種別") == "使用"
        and (not profile_name or row.get("プロファイル名", "") == profile_name)
    ]
    if not matches:
        return None
    for i, _ in sorted(matches, reverse=True):
        ws.delete_rows(i + 2)
    _invalidate(config.SHEET_LEAVE)
    return matches[0][1]


def get_leave_summary(user_id: str, profile_name: str = "") -> list:
    """有給種類ごとの付与・使用・残日数を返す。プロファイル指定時はそのプロファイルのみ集計。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_LEAVE)
    all_records = [r for r in _get_records(ws) if r.get("LINE UserID") == user_id]

    if profile_name:
        records = [r for r in all_records if r.get("プロファイル名", "") == profile_name]
        if not records:
            records = all_records
    else:
        records = all_records

    types: dict = {}
    for r in records:
        t = r.get("種類", "")
        if not t:
            continue
        if t not in types:
            types[t] = {"種類": t, "付与日数": 0.0, "使用日数": 0.0, "有効期限": ""}
        days = float(r.get("日数", 0) or 0)
        if r.get("種別") == "付与":
            types[t]["付与日数"] += days
            if r.get("有効期限"):
                types[t]["有効期限"] = r["有効期限"]
        elif r.get("種別") == "使用":
            types[t]["使用日数"] += days

    result = list(types.values())
    for t in result:
        t["残日数"] = t["付与日数"] - t["使用日数"]
    return result


def get_leave_history(user_id: str, profile_name: str = "", kind: str = "付与") -> list:
    """有給の個別記録（付与または使用）を日付順で返す。プロファイル指定時はそのプロファイル＋
    プロファイル未設定の記録のみ返す。"""
    ss = _get_spreadsheet()
    ws = ss.worksheet(config.SHEET_LEAVE)
    records = [r for r in _get_records(ws) if r.get("LINE UserID") == user_id and r.get("種別") == kind]
    if profile_name:
        records = [r for r in records if r.get("プロファイル名", "") in (profile_name, "")]
    return sorted(records, key=lambda r: r.get("日付", ""))
