import calendar as _cal
import logging
from datetime import datetime, timedelta

import config
import sheets_service
import salary_calculator
import line_service
import user_settings

logger = logging.getLogger(__name__)

# {日付文字列: set(送信済みUserID)} - メモリ上の重複送信防止フラグ
_sent_today: dict[str, set] = {}
# {日付文字列: set("UserID:プロファイル名:締め日")} - 月次レポートの重複防止
_sent_monthly: dict[str, set] = {}


def _cleanup() -> None:
    """当日以外のエントリを削除してメモリを節約する。"""
    today = datetime.now(config.TIMEZONE).strftime("%Y-%m-%d")
    for d in [_sent_today, _sent_monthly]:
        for key in list(d.keys()):
            if key != today:
                del d[key]


def check_and_notify() -> None:
    """APSchedulerから毎分呼び出される。通知条件を満たすユーザーにLINE通知を送る。"""
    _cleanup()
    now = datetime.now(config.TIMEZONE)
    current_time = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y/%m/%d")

    if today not in _sent_today:
        _sent_today[today] = set()

    try:
        users = sheets_service.get_all_users()
    except Exception:
        return

    for user in users:
        if user.get("通知ON/OFF") != "ON":
            continue

        notify_time = str(user.get("通知時刻", config.DEFAULT_NOTIFY_TIME))
        if notify_time != current_time:
            continue

        user_id = user.get("LINE UserID", "")
        if not user_id or user_id in _sent_today[today]:
            continue

        shift = sheets_service.get_shift_by_date(user_id, tomorrow)
        if not shift:
            continue

        work_min = int(shift.get("実働時間(分)", 0))
        try:
            d = datetime.strptime(tomorrow, "%Y/%m/%d")
            weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
        except ValueError:
            weekday = ""

        msg = (
            f"🔔 明日のシフトのお知らせ\n"
            f"📅 {tomorrow}（{weekday}）\n"
            f"⏰ {shift['開始時刻']} 〜 {shift['終了時刻']}\n"
            f"🕐 実働 {salary_calculator.minutes_to_str(work_min)}"
        )

        try:
            line_service.push_text(user_id, msg)
            _sent_today[today].add(user_id)
        except Exception:
            pass


# ── 月次レポート ───────────────────────────────────────

def _is_report_day(cutoff_day: int, today: datetime) -> bool:
    """今日が締め日の翌日かどうかを判定する。cutoff_day=0 は常に False。"""
    if cutoff_day == 0:
        return False
    yesterday = today - timedelta(days=1)
    last_day = _cal.monthrange(yesterday.year, yesterday.month)[1]
    actual_cutoff = min(cutoff_day, last_day)
    return yesterday.day == actual_cutoff


def _build_report_msg(user_id: str, profile_name: str, cutoff_day: int,
                       hourly_wage: int, night_rate: float, early_rate: float,
                       early_end: str, social_insurance: bool) -> str | None:
    """月次レポートメッセージを構築する。期間内にシフトがなければNoneを返す。"""
    now = datetime.now(config.TIMEZONE)
    yesterday = now - timedelta(days=1)
    start_str, end_str, period_label = salary_calculator.get_pay_period(cutoff_day, yesterday)

    shifts = sheets_service.get_shifts_in_period(user_id, start_str, end_str, profile_name)
    if not shifts:
        return None

    result = salary_calculator.aggregate_monthly(shifts, hourly_wage, night_rate, early_rate, early_end)
    allowances = sheets_service.get_allowances(user_id, profile_name)
    result = salary_calculator.apply_allowances(result, shifts, allowances, hourly_wage)
    gross = result["salary"]

    deduction_records = sheets_service.get_deductions(user_id, profile_name)
    pred = salary_calculator.predict_deductions(gross, deduction_records)
    is_estimate = pred is None
    if pred is None:
        pred = salary_calculator.estimate_deductions_default(gross, social_insurance)

    custom_deductions = sheets_service.get_custom_deductions(user_id, profile_name)
    if custom_deductions:
        pred = salary_calculator.apply_custom_deductions(pred, result["work_days"], custom_deductions, gross)

    total_ded = pred.get("合計", 0)
    net = gross - total_ded

    sep = "━" * 16
    profile_tag = f"（{profile_name}）" if profile_name else ""
    ded_label = "概算" if is_estimate else f"過去{pred.get('実績件数', 0)}件の実績より予測"

    lines = [
        f"📊 月次レポート{profile_tag}",
        f"📅 集計期間：{period_label}",
        sep,
        f"勤務日数：{result['work_days']}日",
        f"合計勤務時間：{result['total_str']}",
        sep,
        f"💴 総支給（予測）：¥{gross:,}",
    ]
    if result.get("night_premium", 0) > 0:
        lines.append(f"  🌙 深夜手当：¥{result['night_premium']:,}")
    if result.get("early_premium", 0) > 0 and early_rate > 0:
        lines.append(f"  🌅 早朝手当：¥{result['early_premium']:,}")
    for detail in result.get("allowance_details", []):
        lines.append(f"  🎁 {detail['name']}：¥{detail['amount']:,}")

    lines += [sep, f"📋 控除（{ded_label}）"]
    for key in ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]:
        val = pred.get(key, 0)
        if val > 0:
            lines.append(f"  {key}：¥{val:,}")
    for detail in pred.get("custom_deduction_details", []):
        lines.append(f"  ➖ {detail['name']}：¥{detail['amount']:,}")
    lines += [
        f"  合計控除：¥{total_ded:,}",
        sep,
        f"💰 手取り予測：¥{net:,}",
    ]

    return "\n".join(lines)


def check_and_report() -> None:
    """APSchedulerから毎朝8:00に呼び出される。
    締め日翌日のユーザーに月次シフトレポートをプッシュ送信する。"""
    _cleanup()
    now = datetime.now(config.TIMEZONE)
    today = now.strftime("%Y-%m-%d")

    if today not in _sent_monthly:
        _sent_monthly[today] = set()

    try:
        users = sheets_service.get_all_users()
    except Exception:
        return

    for user in users:
        user_id = user.get("LINE UserID", "")
        if not user_id:
            continue

        try:
            gs = user_settings.get_settings(user_id)
            social_insurance = (gs.get("社会保険加入", "なし") or "なし") == "あり"
            g_wage = int(gs.get("時給(円)", config.DEFAULT_HOURLY_WAGE) or config.DEFAULT_HOURLY_WAGE)
            night_rate = float(gs.get("深夜割増率", config.DEFAULT_NIGHT_RATE) or config.DEFAULT_NIGHT_RATE) / 100
            early_rate = float(gs.get("早朝割増率", config.DEFAULT_EARLY_RATE) or config.DEFAULT_EARLY_RATE) / 100
            early_end = gs.get("早朝終了時刻", config.DEFAULT_EARLY_END) or config.DEFAULT_EARLY_END

            profiles = sheets_service.get_profiles(user_id)

            for p in profiles:
                cutoff_day = int(p.get("締め日") or 0)
                if cutoff_day == 0:
                    continue  # 締め日未設定のプロファイルはスキップ

                profile_name = p.get("プロファイル名", "")
                p_wage = int(p.get("時給(円)") or g_wage)

                if not _is_report_day(cutoff_day, now):
                    continue

                dedup_key = f"{user_id}:{profile_name}:{cutoff_day}"
                if dedup_key in _sent_monthly[today]:
                    continue

                msg = _build_report_msg(
                    user_id, profile_name, cutoff_day,
                    p_wage, night_rate, early_rate, early_end, social_insurance,
                )
                if msg:
                    line_service.push_text(user_id, msg)
                    _sent_monthly[today].add(dedup_key)

        except Exception as e:
            logger.error(f"月次レポートエラー (user_id={user_id}): {e}")
            continue
