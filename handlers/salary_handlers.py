"""給与確認・有効設定の解決・全データ削除フローを扱うハンドラー群。

`_get_effective_settings` / `_get_premium_settings` / `_resolve_color` は、
他の多くのハンドラー（シフト登録・有給・明細）から共通で参照される「基盤」関数のため、
このモジュールは他のhandlersモジュールに依存しない（循環import防止）。
"""

import json
from datetime import datetime

import config
import flex_builder
import line_service
import salary_calculator
import sheets_service
import user_settings


def get_premium_settings(user_id: str) -> dict:
    """深夜・早朝手当の設定をユーザー設定から取得する。"""
    gs = user_settings.get_settings(user_id)
    return {
        "night_rate": float(gs.get("深夜割増率", config.DEFAULT_NIGHT_RATE) or config.DEFAULT_NIGHT_RATE) / 100,
        "early_rate": float(gs.get("早朝割増率", config.DEFAULT_EARLY_RATE) or config.DEFAULT_EARLY_RATE) / 100,
        "early_end":  gs.get("早朝終了時刻", config.DEFAULT_EARLY_END) or config.DEFAULT_EARLY_END,
    }


def resolve_color(color_name: str) -> str:
    """色名をGoogleカレンダーのcolorIdに変換する。"""
    return config.CALENDAR_COLOR_MAP.get((color_name or "").strip(), "")


def get_effective_settings(user_id: str, label: str | None) -> dict:
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


def estimate_current_gross(user_id: str, profile_name: str | None = None) -> int | None:
    """現在の給与サイクルの総支給額（概算）を返す。締め日未設定などで算出できない場合はNoneを返す。"""
    eff = get_effective_settings(user_id, profile_name)
    if eff["cutoff_day"] == 0:
        return None

    ps = get_premium_settings(user_id)
    now = datetime.now(config.TIMEZONE)
    start_str, end_str, _ = salary_calculator.get_pay_period(eff["cutoff_day"], now)
    shifts = sheets_service.get_shifts_in_period(user_id, start_str, end_str, eff["profile_name"])

    result = salary_calculator.aggregate_monthly(
        shifts, eff["hourly_wage"], ps["night_rate"], ps["early_rate"], ps["early_end"]
    )
    allowances = sheets_service.get_allowances(user_id, eff["profile_name"])
    result = salary_calculator.apply_allowances(result, shifts, allowances, eff["hourly_wage"])
    return result["salary"] or None


def handle_salary(event, user_id: str, parsed: dict = None):
    now = datetime.now(config.TIMEZONE)
    profile_name = parsed.get("profile_name") if parsed else None
    eff = get_effective_settings(user_id, profile_name)

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
    ps = get_premium_settings(user_id)

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


def handle_delete_all(event, user_id: str) -> None:
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


def confirm_delete_all_stage1(reply_token: str) -> None:
    """全データ削除の第2段階（最終）確認。"""
    line_service.reply_with_quickreply(reply_token,
        "🚨 最終確認（2/2）\n\n"
        "本当にすべてのデータを完全に削除しますか？\n\n"
        "削除後は元に戻すことができません。\n"
        "Googleカレンダーとの連携は解除されますが、カレンダー上の予定はそのまま残ります。", [
            {"label": "🗑️ 完全に削除する", "data": json.dumps({"action": "delete_all_stage2"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ])


def confirm_delete_all_stage2(user_id: str, reply_token: str) -> None:
    """全データ削除を実行する。"""
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
        import logging
        logging.getLogger(__name__).error(f"全データ削除エラー: {e}")
        line_service.push_text(user_id,
            "削除中にエラーが発生しました。一部のデータが残っている可能性があります。")
