from datetime import datetime
import salary_calculator as sc


def _text(text, size="sm", color=None, weight=None, flex=None, align=None, wrap=False):
    obj = {"type": "text", "text": str(text), "size": size}
    if color:
        obj["color"] = color
    if weight:
        obj["weight"] = weight
    if flex is not None:
        obj["flex"] = flex
    if align:
        obj["align"] = align
    if wrap:
        obj["wrap"] = True
    return obj


def _row(label, value, value_color=None, bold_value=False):
    return {
        "type": "box",
        "layout": "horizontal",
        "paddingTop": "4px",
        "paddingBottom": "4px",
        "contents": [
            _text(label, color="#555555", flex=3),
            _text(value, align="end", flex=2,
                  color=value_color or "#111111",
                  weight="bold" if bold_value else None),
        ],
    }


def _sep():
    return {"type": "separator", "margin": "sm", "color": "#EEEEEE"}


def build_shift_list(shifts: list, period_label: str) -> dict:
    """シフト一覧のFlex Message contentsを構築する。"""
    total_min = sum(int(s.get("実働時間(分)", 0)) for s in shifts)
    total_str = sc.minutes_to_str(total_min)

    body_contents = []
    for i, s in enumerate(shifts):
        if i > 0:
            body_contents.append(_sep())
        try:
            d = datetime.strptime(s["日付"], "%Y/%m/%d")
            weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
            date_label = f"{d.month}/{d.day}({weekday})"
        except (ValueError, KeyError):
            date_label = s.get("日付", "")

        work_min = int(s.get("実働時間(分)", 0))
        h, m = divmod(work_min, 60)
        work_short = f"{h}h{m:02d}m" if m else f"{h}h"

        body_contents.append({
            "type": "box",
            "layout": "horizontal",
            "paddingTop": "6px",
            "paddingBottom": "6px",
            "contents": [
                _text(date_label, weight="bold", flex=3),
                _text(f"{s.get('開始時刻', '')}〜{s.get('終了時刻', '')}", flex=4, color="#444444"),
                _text(work_short, flex=2, align="end", color="#27AE60"),
            ],
        })

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "backgroundColor": "#27AE60",
            "contents": [
                _text(f"📅 {period_label}のシフト", size="lg", weight="bold", color="#ffffff"),
                _text(f"{len(shifts)}日間  /  {total_str}", size="sm", color="#ffffffBB"),
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": body_contents,
        },
    }


def build_salary_summary(result: dict, period_label: str, hourly_wage: int,
                          pred: dict, ps: dict, payday_note: str = "") -> dict:
    """給与サマリーのFlex Message contentsを構築する。"""
    body = []

    body.append({
        "type": "box",
        "layout": "horizontal",
        "paddingBottom": "12px",
        "contents": [
            {
                "type": "box", "layout": "vertical", "flex": 1,
                "contents": [
                    _text("勤務日数", size="xs", color="#888888"),
                    _text(f"{result['work_days']}日", size="xl", weight="bold"),
                ],
            },
            {
                "type": "box", "layout": "vertical", "flex": 1,
                "contents": [
                    _text("実働時間", size="xs", color="#888888"),
                    _text(result["total_str"], size="xl", weight="bold"),
                ],
            },
        ],
    })
    body.append(_sep())

    body.append(_row("基本給", f"¥{result['base_pay']:,}"))
    if result.get("night_premium", 0) > 0:
        body.append(_row(f"深夜手当（{ps['night_rate']*100:.0f}%）",
                         f"+¥{result['night_premium']:,}", value_color="#E67E22"))
    if result.get("early_premium", 0) > 0:
        body.append(_row(f"早朝手当（{ps['early_rate']*100:.0f}%）",
                         f"+¥{result['early_premium']:,}", value_color="#E67E22"))
    for d in result.get("allowance_details", []):
        body.append(_row(f"🎁 {d['name']}", f"+¥{d['amount']:,}", value_color="#E67E22"))

    body.append(_sep())
    body.append(_row("総支給", f"¥{result['salary']:,}", bold_value=True))
    body.append(_sep())

    for key in ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]:
        if pred.get(key, 0) > 0:
            body.append(_row(key, f"-¥{pred[key]:,}", value_color="#E74C3C"))
    for d in pred.get("variable_deductions", []):
        if d.get("predicted", 0) > 0:
            months_note = f"（過去{d['months']}か月平均）" if d.get("months", 0) > 1 else ""
            body.append(_row(f"{d['name']}{months_note}", f"-¥{d['predicted']:,}", value_color="#E74C3C"))
    for d in pred.get("custom_deduction_details", []):
        body.append(_row(f"➖ {d['name']}", f"-¥{d['amount']:,}", value_color="#E74C3C"))

    body.append(_sep())
    body.append(_row("手取り予測", f"¥{pred['手取り予測']:,}",
                     value_color="#27AE60", bold_value=True))

    note = (f"※過去{pred['実績件数']}か月の明細を参考に算出"
            if not pred.get("概算") else "※標準レートによる概算")
    body.append(_text(note, size="xs", color="#AAAAAA"))

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "backgroundColor": "#2980B9",
            "contents": [
                _text(f"💴 {period_label}の給与予測", size="lg", weight="bold", color="#ffffff"),
                _text(f"時給 ¥{hourly_wage:,}", size="sm", color="#ffffffBB"),
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": body,
        },
    }
    if payday_note:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [_text(f"💳 給料日：{payday_note}", size="sm", color="#555555")],
        }
    return bubble


def build_leave_summary(leave_data: list, profile_name: str = "") -> dict:
    """有給残日数のFlex Message contentsを構築する。"""
    body = []
    for i, lt in enumerate(leave_data):
        if i > 0:
            body.append(_sep())
        granted = float(lt.get("付与日数", 0) or 0)
        used = float(lt.get("使用日数", 0) or 0)
        remaining = granted - used
        leave_type = lt.get("種類", "")
        expiry = lt.get("有効期限", "") or ""

        color = "#27AE60" if remaining > 0 else "#E74C3C"
        sub_contents = [
            _text(f"付与 {granted:g}日  /  使用 {used:g}日", size="xs", color="#888888", flex=3),
        ]
        if expiry:
            sub_contents.append(_text(f"期限 {expiry}", size="xs", color="#AAAAAA", align="end", flex=2))

        body.append({
            "type": "box",
            "layout": "vertical",
            "paddingTop": "8px",
            "paddingBottom": "8px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        _text(leave_type, weight="bold", flex=3),
                        _text(f"残 {remaining:g}日", align="end", flex=2,
                              color=color, weight="bold"),
                    ],
                },
                {"type": "box", "layout": "horizontal", "contents": sub_contents},
            ],
        })

    if not body:
        body = [_text(
            "有給がまだ登録されていません。\n「有給が10日付与されました」と送ってください。",
            wrap=True, color="#888888",
        )]

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "backgroundColor": "#8E44AD",
            "contents": [
                _text("🌿 有給残日数", size="lg", weight="bold", color="#ffffff"),
            ] + ([_text(profile_name, size="xs", color="#D9C7F0")] if profile_name else []) + [
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": body,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [
                _text("「有給付与履歴」で付与日ごとの内訳を確認できます", size="xxs", color="#AAAAAA", wrap=True),
            ],
        },
    }
