"""仕事名（プロファイル）の作成・切替・更新・一覧・削除を扱うハンドラー群。"""

import json
import logging

import line_service
import sheets_service
import user_settings

logger = logging.getLogger(__name__)


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


# ── ポストバック（ボタン確定後の実処理） ─────────────────────

def switch_profile_direct(user_id: str, reply_token: str, data: dict) -> None:
    name = data.get("name", "")
    if not sheets_service.get_profile(user_id, name):
        line_service.reply_text(reply_token, f"仕事名「{name}」が見つかりません。\nまず登録してください。")
        return
    user_settings.update_setting(user_id, "active_profile", name)
    line_service.reply_text(reply_token,
        f"✅ 「{name}」に切り替えました。\n以降のシフト登録はこの仕事名の設定が使われます。")


def select_delete_profile(user_id: str, reply_token: str, data: dict) -> None:
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


def delete_profile_stage1(reply_token: str, data: dict) -> None:
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


def confirm_delete_profile(user_id: str, reply_token: str, data: dict) -> None:
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
