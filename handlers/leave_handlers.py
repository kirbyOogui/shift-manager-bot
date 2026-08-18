"""有給の付与・取得・確認・履歴・削除・修正を扱うハンドラー群。"""

import json
import logging
from datetime import datetime, timedelta

import calendar_service
import flex_builder
import line_service
import salary_calculator
import sheets_service
import state
from handlers import salary_handlers

logger = logging.getLogger(__name__)


def _handle_grant_leave(event, user_id: str, parsed: dict):
    leave_type = parsed.get("type") or "年次有給"
    days = float(parsed.get("days") or 0)
    if days <= 0:
        line_service.reply_text(event.reply_token, "付与日数を指定してください。\n例：有給が10日付与されました")
        return
    granted_date = parsed.get("granted_date") or ""
    expiry_date = parsed.get("expiry_date") or ""
    note = parsed.get("note") or ""

    eff = salary_handlers.get_effective_settings(user_id, None)
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

    eff = salary_handlers.get_effective_settings(user_id, None)
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

    state.pending_leave_usage[user_id] = {
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
    eff = salary_handlers.get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    leave_data = sheets_service.get_leave_summary(user_id, profile_name)
    contents = flex_builder.build_leave_summary(leave_data, profile_name)
    line_service.reply_flex(event.reply_token, f"有給残日数（{profile_name}）" if profile_name else "有給残日数", contents)


def _handle_leave_history(event, user_id: str):
    """有給の付与履歴を日付順で一覧表示する。"""
    eff = salary_handlers.get_effective_settings(user_id, None)
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

    eff = salary_handlers.get_effective_settings(user_id, None)
    profile_name = eff["profile_name"]
    shift = sheets_service.get_shift_by_date(user_id, date_str)

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
    state.pending_delete_leave[user_id] = {
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

    eff = salary_handlers.get_effective_settings(user_id, None)
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
    state.pending_modify_leave[user_id] = {
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


# ── ポストバック（ボタン確定後の実処理） ─────────────────────

def confirm_use_leave(user_id: str, reply_token: str) -> None:
    pending = state.pending_leave_usage.pop(user_id, None)
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
                color_id = salary_handlers.resolve_color(pending.get("color", ""))
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


def confirm_delete_leave(user_id: str, reply_token: str) -> None:
    pending = state.pending_delete_leave.pop(user_id, None)
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


def confirm_modify_leave(user_id: str, reply_token: str) -> None:
    pending = state.pending_modify_leave.pop(user_id, None)
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
            color_id = salary_handlers.resolve_color(eff.get("color", ""))
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
