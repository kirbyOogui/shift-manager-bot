"""給与明細画像の読み取り、カスタム手当・カスタム控除・明細データのCRUDを扱うハンドラー群。"""

import calendar as _cal
import json
import logging
from datetime import datetime

import config
import line_service
import sheets_service
import state
import user_settings
from handlers import salary_handlers

logger = logging.getLogger(__name__)

_TYPE_LABEL = {"月額固定": "月額固定", "日数比例": "日数比例", "期間割増": "期間割増", "時間単価": "時間単価"}
_DEDUCTION_TYPE_LABEL = {"固定": "固定", "日数比例": "日数比例", "定率": "定率"}


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
    state.pending_deductions[user_id] = {
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
    pending = state.pending_payslip_deductions.get(user_id)
    if not pending or not pending["items"]:
        state.pending_payslip_deductions.pop(user_id, None)
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

    state.pending_payslip_allowances[user_id] = {
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
    state.pending_del_allowance[user_id] = {"name": name}
    line_service.reply_with_quickreply(event.reply_token,
        f"手当「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_allowance"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


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
            gross = salary_handlers.estimate_current_gross(user_id, parsed.get("profile"))
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
    state.pending_del_custom_deduction[user_id] = {"name": name}
    line_service.reply_with_quickreply(event.reply_token,
        f"控除「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_custom_deduction"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


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
    state.pending_deductions[user_id] = {"year_month": year_month, "gross": gross, "items": items, "profile_name": active_profile}
    line_service.reply_with_quickreply(event.reply_token, "\n".join(lines), [
        {"label": "✅ 登録する", "data": json.dumps({"action": "confirm_deductions"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_delete_deduction(event, user_id: str, parsed: dict):
    now = datetime.now(config.TIMEZONE)
    year_month = parsed.get("year_month") or now.strftime("%Y/%m")
    eff = salary_handlers.get_effective_settings(user_id, None)
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
    state.pending_delete_deductions[user_id] = {"year_month": year_month, "profile_name": profile_name}
    line_service.reply_with_quickreply(event.reply_token, msg, [
        {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_deductions"})},
        {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
    ])


def _handle_modify_deduction(event, user_id: str, parsed: dict):
    now = datetime.now(config.TIMEZONE)
    year_month = parsed.get("year_month") or now.strftime("%Y/%m")
    field_key = parsed.get("field", "")
    value = parsed.get("value")
    eff = salary_handlers.get_effective_settings(user_id, None)
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


# ── ポストバック（ボタン確定後の実処理） ─────────────────────

def confirm_deductions(user_id: str, reply_token: str) -> None:
    pending = state.pending_deductions.pop(user_id, None)
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
            state.pending_payslip_deductions[user_id] = {
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


def confirm_delete_deductions(user_id: str, reply_token: str) -> None:
    pending = state.pending_delete_deductions.pop(user_id, None)
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


def confirm_modify_deductions(user_id: str, reply_token: str, data: dict) -> None:
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


def select_delete_allowance(user_id: str, reply_token: str, data: dict) -> None:
    name = data.get("name", "")
    if not any(a.get("手当名") == name for a in sheets_service.get_allowances(user_id)):
        line_service.reply_text(reply_token, f"手当「{name}」が見つかりませんでした。")
        return
    state.pending_del_allowance[user_id] = {"name": name}
    line_service.reply_with_quickreply(reply_token,
        f"手当「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_allowance"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


def confirm_delete_allowance(user_id: str, reply_token: str) -> None:
    name = state.pending_del_allowance.pop(user_id, {}).get("name", "")
    if name and sheets_service.delete_allowance(user_id, name):
        line_service.reply_text(reply_token, f"🗑️ 手当「{name}」を削除しました。")
    else:
        line_service.reply_text(reply_token, "削除に失敗しました。")


def select_delete_custom_deduction(user_id: str, reply_token: str, data: dict) -> None:
    name = data.get("name", "")
    if not any(d.get("控除名") == name for d in sheets_service.get_custom_deductions(user_id)):
        line_service.reply_text(reply_token, f"控除「{name}」が見つかりませんでした。")
        return
    state.pending_del_custom_deduction[user_id] = {"name": name}
    line_service.reply_with_quickreply(reply_token,
        f"控除「{name}」を削除しますか？",
        [
            {"label": "🗑️ 削除する", "data": json.dumps({"action": "confirm_delete_custom_deduction"})},
            {"label": "❌ キャンセル", "data": json.dumps({"action": "cancel"})},
        ]
    )


def confirm_delete_custom_deduction(user_id: str, reply_token: str) -> None:
    name = state.pending_del_custom_deduction.pop(user_id, {}).get("name", "")
    if name and sheets_service.delete_custom_deduction(user_id, name):
        line_service.reply_text(reply_token, f"🗑️ 控除「{name}」を削除しました。")
    else:
        line_service.reply_text(reply_token, "削除に失敗しました。")


def select_payslip_allowance(user_id: str, reply_token: str, data: dict) -> None:
    pending = state.pending_payslip_allowances.get(user_id)
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


def register_payslip_allowance(user_id: str, reply_token: str, data: dict) -> None:
    pending = state.pending_payslip_allowances.get(user_id)
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
    state.pending_payslip_allowances[user_id]["allowances"] = remaining
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
        state.pending_payslip_allowances.pop(user_id, None)
        line_service.reply_text(reply_token,
            f"✅ 「{a['name']}」を{reg_desc}のカスタム手当として登録しました。\n"
            "給与予測に毎月反映されます。\n\n"
            "手当の確認・変更は「手当一覧」から行えます。"
        )


def select_payslip_deduction(user_id: str, reply_token: str, data: dict) -> None:
    pending = state.pending_payslip_deductions.get(user_id)
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


def register_payslip_deduction(user_id: str, reply_token: str, data: dict) -> None:
    pending = state.pending_payslip_deductions.get(user_id)
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


def skip_allowance(user_id: str, reply_token: str) -> None:
    state.pending_payslip_allowances.pop(user_id, None)
    line_service.reply_text(reply_token, "手当の登録をスキップしました。\n後から「手当を追加して」と送っていつでも登録できます。")


def skip_payslip_deductions(user_id: str, reply_token: str) -> None:
    pending = state.pending_payslip_deductions.get(user_id)
    if pending:
        pending["items"] = []
    line_service.reply_text(reply_token, "控除の登録をスキップしました。\n後から「控除を追加して」と送っていつでも登録できます。")
    _offer_next_payslip_deduction(user_id)
