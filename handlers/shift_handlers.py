"""シフトの登録・修正・削除・一覧、および写真からのシフト表読み取りを扱うハンドラー群。"""

import json
import logging
from datetime import datetime

import calendar_service
import config
import flex_builder
import line_service
import salary_calculator
import sheets_service
import shift_parser
import state
import user_settings
from handlers import payslip_handlers, salary_handlers

logger = logging.getLogger(__name__)


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

    eff = salary_handlers.get_effective_settings(user_id, None)
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
            "ct": eff["calendar_title"], "ci": salary_handlers.resolve_color(eff["color"]), "pn": eff["profile_name"],
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

    state.pending_multi_shifts[user_id] = {"batch": resolved}
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
        payslip_handlers._handle_image_payslip(event, user_id, result)
        return

    if image_type != "shift":
        line_service.reply_text(reply_token, "シフト表か給与明細の画像を送ってください。")
        return

    shifts = result.get("shifts", [])
    if not shifts:
        detected_names = result.get("detected_names", [])
        if employee_name and not result.get("employee_found", True) and detected_names:
            # b64・structure を保存して選択後に最初から再解析できるようにする
            state.pending_name_selection[user_id] = {
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
    eff = salary_handlers.get_effective_settings(user_id, None)
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

    state.pending_update_shifts[user_id] = {
        "date": date_str, "start": new_start, "end": new_end,
        "break": effective_break, "work_min": work_min,
    }

    line_service.reply_with_quickreply(event.reply_token, confirm_text, [
        {"label": "✅ 変更する", "data": json.dumps({"action": "confirm_update_shift"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_register(event, parsed: dict, user_id: str):
    label = parsed.get("title")
    eff = salary_handlers.get_effective_settings(user_id, label)

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
    color_id = salary_handlers.resolve_color(effective_color_name)
    calendar_title = eff["calendar_title"]
    display_color = effective_color_name or "デフォルト"

    ps = salary_handlers.get_premium_settings(user_id)
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
    eff = salary_handlers.get_effective_settings(user_id, label)

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
    color_id = salary_handlers.resolve_color(effective_color_name)
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

    state.pending_multi_shifts[user_id] = {
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
        eff = salary_handlers.get_effective_settings(user_id, label)

        date_str  = s.get("date", "")
        start_time = s.get("start_time") or eff["default_start"]
        end_time   = s.get("end_time")   or eff["default_end"]
        parsed_break = s.get("break_minutes")
        effective_break = int(parsed_break) if parsed_break is not None else eff["break_minutes"]
        msg_color = s.get("color")
        effective_color_name = msg_color or eff["color"]
        color_id = salary_handlers.resolve_color(effective_color_name)
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

    state.pending_multi_shifts[user_id] = {"batch": resolved}

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
    eff = salary_handlers.get_effective_settings(user_id, profile_name)

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


# ── ポストバック（ボタン確定後の実処理） ─────────────────────

def confirm_register(user_id: str, reply_token: str, data: dict) -> None:
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


def confirm_register_multi(user_id: str, reply_token: str) -> None:
    pending = state.pending_multi_shifts.pop(user_id, None)
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


def confirm_delete(user_id: str, reply_token: str, data: dict) -> None:
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


def confirm_update_shift(user_id: str, reply_token: str) -> None:
    pending = state.pending_update_shifts.pop(user_id, None)
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
        eff = salary_handlers.get_effective_settings(user_id, None)
        try:
            calendar_service.update_event(
                user_id, event_id, date_str, new_start, new_end,
                summary=eff["calendar_title"], color_id=salary_handlers.resolve_color(eff["color"]),
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


def select_shift_name(user_id: str, reply_token: str, data: dict) -> None:
    pending = state.pending_name_selection.pop(user_id, None)
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
