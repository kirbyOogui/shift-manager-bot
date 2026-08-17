import calendar
from datetime import datetime


def get_pay_period_for_month(cutoff_day: int, year: int, month: int) -> tuple:
    """指定年月を含む締め日ベースの集計期間を返す。cutoff_day=0 はカレンダー月。
    例: cutoff_day=20, year=2026, month=5 → 2026/04/21〜2026/05/20"""
    if not cutoff_day:
        last = calendar.monthrange(year, month)[1]
        return f"{year}/{month:02d}/01", f"{year}/{month:02d}/{last:02d}", f"{year}年{month}月"

    end_day = min(cutoff_day, calendar.monthrange(year, month)[1])
    end_str = f"{year}/{month:02d}/{end_day:02d}"

    py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
    prev_last = calendar.monthrange(py, pm)[1]
    s_day = cutoff_day + 1
    if s_day > prev_last:
        sy, sm_val, s_day = year, month, 1
    else:
        sy, sm_val = py, pm
    start_str = f"{sy}/{sm_val:02d}/{s_day:02d}"

    def _fmt(s): return f"{int(s[5:7])}/{int(s[8:10])}"
    label = f"{year}年{month}月分（{_fmt(start_str)}〜{_fmt(end_str)}）"
    return start_str, end_str, label


def get_pay_period(cutoff_day: int, ref_date: datetime) -> tuple:
    """締め日に基づく集計期間を返す。(start_str, end_str, label) の形式。
    cutoff_day=0 はカレンダー月。"""
    if not cutoff_day:
        y, m = ref_date.year, ref_date.month
        last = calendar.monthrange(y, m)[1]
        return f"{y}/{m:02d}/01", f"{y}/{m:02d}/{last:02d}", f"{y}年{m}月"

    if ref_date.day <= cutoff_day:
        # 前月締め日+1日〜今月締め日
        y, m = ref_date.year, ref_date.month
        end_day = min(cutoff_day, calendar.monthrange(y, m)[1])
        end_str = f"{y}/{m:02d}/{end_day:02d}"

        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        prev_last = calendar.monthrange(py, pm)[1]
        s_day = cutoff_day + 1
        if s_day > prev_last:
            sy, sm, s_day = y, m, 1
        else:
            sy, sm = py, pm
        start_str = f"{sy}/{sm:02d}/{s_day:02d}"
    else:
        # 今月締め日+1日〜来月締め日
        y, m = ref_date.year, ref_date.month
        s_day = cutoff_day + 1
        this_last = calendar.monthrange(y, m)[1]
        if s_day > this_last:
            ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
            start_str = f"{ny}/{nm:02d}/01"
        else:
            start_str = f"{y}/{m:02d}/{s_day:02d}"
        ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
        end_day = min(cutoff_day, calendar.monthrange(ny, nm)[1])
        end_str = f"{ny}/{nm:02d}/{end_day:02d}"

    def _fmt(s): return f"{int(s[5:7])}/{int(s[8:10])}"
    label = f"{_fmt(start_str)}〜{_fmt(end_str)}（締め{cutoff_day}日）"
    return start_str, end_str, label


def _time_to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _overlap(s1: int, e1: int, s2: int, e2: int) -> int:
    return max(0, min(e1, e2) - max(s1, s2))


def calc_work_minutes(start_time: str, end_time: str, break_minutes: int) -> int:
    """実働時間（分）を計算する。日をまたぐシフトにも対応。"""
    start = datetime.strptime(start_time, "%H:%M")
    end = datetime.strptime(end_time, "%H:%M")
    diff_minutes = (end - start).total_seconds() / 60
    if diff_minutes < 0:
        diff_minutes += 24 * 60
    return max(0, int(diff_minutes) - break_minutes)


def calc_premium_minutes(start_str: str, end_str: str, early_end_min: int = 480) -> dict:
    """深夜（22:00〜5:00）・早朝（5:00〜early_end）の勤務分（休憩前）を計算する。"""
    s = _time_to_min(start_str)
    e = _time_to_min(end_str)
    if e <= s:
        e += 1440

    # 深夜 22:00〜翌5:00 を3区間に分けて処理
    # [1320,1440]: 22:00〜24:00（当日）
    # [1440,1740]: 翌0:00〜翌5:00（日またぎシフト用）
    # [0,300]:     0:00〜5:00（未調整シフト用、例: 2:00〜8:00）
    night = (
        _overlap(s, e, 1320, 1440) +
        _overlap(s, e, 1440, 1740) +
        _overlap(s, e, 0, 300)
    )
    # 早朝 5:00〜early_end（翌日分も考慮）
    early = (
        _overlap(s, e, 300, early_end_min) +
        _overlap(s, e, 300 + 1440, early_end_min + 1440)
    )
    return {"night": night, "early": early, "total_raw": e - s}


def calc_shift_summary(start_str: str, end_str: str, break_min: int,
                        wage: int, night_rate: float, early_rate: float,
                        early_end_str: str = "08:00") -> dict:
    """1シフトの給与内訳を計算する。"""
    work_min = calc_work_minutes(start_str, end_str, break_min)
    early_end_min = _time_to_min(early_end_str)
    pm = calc_premium_minutes(start_str, end_str, early_end_min)
    total_raw = pm["total_raw"]

    if total_raw > 0:
        night_work = round(pm["night"] * work_min / total_raw)
        early_work = round(pm["early"] * work_min / total_raw)
    else:
        night_work = early_work = 0

    wage_per_min = wage / 60
    base_pay     = round(work_min * wage_per_min)
    night_premium = round(night_work * wage_per_min * night_rate)
    early_premium = round(early_work * wage_per_min * early_rate)

    return {
        "work_min": work_min,
        "night_min": night_work,
        "early_min": early_work,
        "base_pay": base_pay,
        "night_premium": night_premium,
        "early_premium": early_premium,
        "total": base_pay + night_premium + early_premium,
    }


def minutes_to_str(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h}時間{m}分" if m > 0 else f"{h}時間"


def calc_salary(work_minutes: int, hourly_wage: int) -> int:
    return int((work_minutes / 60) * hourly_wage)


def _estimate_income_tax(gross: int) -> int:
    """月収から所得税を概算する（甲欄・扶養0人ベース）。"""
    if gross < 88_000:
        return 0
    elif gross < 162_500:
        return round(gross * 0.015)
    elif gross < 275_000:
        return round(gross * 0.04)
    else:
        return round(gross * 0.07)


def estimate_deductions_default(gross: int, social_insurance: bool = False) -> dict:
    """実績データがない場合の標準レートによる概算控除額を返す。"""
    items = {}
    items["雇用保険"] = round(gross * 0.006)
    items["所得税"]   = _estimate_income_tax(gross)
    if social_insurance:
        items["健康保険"] = round(gross * 0.05)
        items["厚生年金"] = round(gross * 0.0915)
    total = sum(items.values())
    return {**items, "合計": total, "手取り予測": gross - total, "実績件数": 0, "概算": True}


def _date_in_period(month: int, day: int, sm: int, sd: int, em: int, ed: int) -> bool:
    """月日が期間内かチェック。年またぎ（12/28〜1/4等）に対応。"""
    check = month * 100 + day
    start = sm * 100 + sd
    end   = em * 100 + ed
    if start <= end:
        return start <= check <= end
    else:  # 年またぎ
        return check >= start or check <= end


def apply_allowances(result: dict, shifts: list, allowances: list, hourly_wage: int) -> dict:
    """カスタム手当を給与に適用する。"""
    extra = 0
    details = []

    for a in allowances:
        if str(a.get("有効", "yes")).lower() in ("no", "false", "0", "いいえ", "無効"):
            continue
        atype = a.get("タイプ", "")
        name  = a.get("手当名", "")

        if atype == "月額固定":
            amount = int(a.get("金額", 0) or 0)
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount, "note": "月額固定"})

        elif atype == "日数比例":
            unit = int(a.get("金額", 0) or 0)
            amount = unit * result["work_days"]
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount,
                                 "note": f"{result['work_days']}日×¥{unit:,}"})

        elif atype == "期間割増":
            rate = float(a.get("割増率(%)", 0) or 0) / 100
            sm = int(a.get("期間開始月", 0) or 0)
            sd = int(a.get("期間開始日", 0) or 0)
            em = int(a.get("期間終了月", 0) or 0)
            ed = int(a.get("期間終了日", 0) or 0)
            if not (rate and sm and sd and em and ed):
                continue
            premium = 0
            hit_days = 0
            for s in shifts:
                try:
                    d = datetime.strptime(s["日付"], "%Y/%m/%d")
                    if _date_in_period(d.month, d.day, sm, sd, em, ed):
                        work_min = int(s.get("実働時間(分)", 0))
                        premium += round(work_min / 60 * hourly_wage * rate)
                        hit_days += 1
                except (ValueError, KeyError):
                    continue
            if premium:
                extra += premium
                details.append({"name": name, "amount": premium,
                                 "note": f"{hit_days}日分 {int(rate*100)}%割増"})

        elif atype == "時間単価":
            unit = int(a.get("金額", 0) or 0)
            amount = round(result["total_minutes"] / 60 * unit)
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount,
                                 "note": f"¥{unit:,}/時間"})

    result = dict(result)
    result["allowance_total"]   = extra
    result["allowance_details"] = details
    result["salary"]            = result["salary"] + extra
    return result


def apply_custom_deductions(pred: dict, work_days: int, deductions: list, gross: int = 0) -> dict:
    """カスタム控除（日数比例／固定／定率）を給与予測に適用する。"""
    extra = 0
    details = []

    for d in deductions:
        if str(d.get("有効", "yes")).lower() in ("no", "false", "0", "いいえ", "無効"):
            continue
        dtype = d.get("タイプ", "")
        name = d.get("控除名", "")

        if dtype == "固定":
            amount = int(d.get("金額", 0) or 0)
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount, "note": "固定"})

        elif dtype == "日数比例":
            unit = int(d.get("金額", 0) or 0)
            amount = unit * work_days
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount,
                                 "note": f"{work_days}日×¥{unit:,}"})

        elif dtype == "定率":
            rate = float(d.get("率(%)", 0) or 0) / 100
            amount = round(gross * rate)
            if amount:
                extra += amount
                details.append({"name": name, "amount": amount,
                                 "note": f"総支給額の{d.get('率(%)', 0)}%"})

    result = dict(pred)
    result["custom_deduction_total"]   = extra
    result["custom_deduction_details"] = details
    result["合計"]       = result.get("合計", 0) + extra
    result["手取り予測"] = result.get("手取り予測", 0) - extra
    return result


def predict_deductions(gross: int, records: list) -> dict | None:
    """過去の控除実績から平均率を算出し予測控除額を返す。実績なしはNoneを返す。"""
    import json as _json
    from collections import defaultdict

    fields = ["健康保険", "介護保険", "厚生年金", "雇用保険", "所得税", "住民税", "その他"]
    valid = [r for r in records if int(r.get("総支給額", 0) or 0) > 0]
    if not valid:
        return None

    rates = {f: sum(int(r.get(f, 0) or 0) / int(r["総支給額"]) for r in valid) / len(valid) for f in fields}
    result = {f: round(gross * rates[f]) for f in fields}

    # 変動控除（名前付き）: 過去実績の平均額を予測値とする
    var_buckets: dict = defaultdict(list)
    for r in valid:
        raw = r.get("変動控除(JSON)", "")
        if not raw:
            continue
        try:
            for item in _json.loads(raw):
                name = item.get("name", "").strip()
                amount = int(item.get("amount") or 0)
                if name and amount > 0:
                    var_buckets[name].append(amount)
        except Exception:
            pass

    variable_deductions = [
        {"name": name, "predicted": round(sum(amounts) / len(amounts)), "months": len(amounts)}
        for name, amounts in var_buckets.items()
    ]

    var_total = sum(d["predicted"] for d in variable_deductions)
    result["variable_deductions"] = variable_deductions
    result["合計"] = sum(result[f] for f in fields) + var_total
    result["手取り予測"] = gross - result["合計"]
    result["実績件数"] = len(valid)
    return result


def aggregate_monthly(shifts: list, hourly_wage: int,
                       night_rate: float = 0.25, early_rate: float = 0.0,
                       early_end_str: str = "08:00") -> dict:
    """月次集計を割増賃金込みで返す。"""
    early_end_min = _time_to_min(early_end_str)
    total_minutes = total_night = total_early = 0

    for s in shifts:
        work_min   = int(s.get("実働時間(分)", 0))
        start_str  = s.get("開始時刻", "")
        end_str    = s.get("終了時刻", "")
        if start_str and end_str and work_min > 0:
            pm = calc_premium_minutes(start_str, end_str, early_end_min)
            total_raw = pm["total_raw"]
            if total_raw > 0:
                night_work = round(pm["night"] * work_min / total_raw)
                early_work = round(pm["early"] * work_min / total_raw)
            else:
                night_work = early_work = 0
        else:
            night_work = early_work = 0
        total_minutes += work_min
        total_night   += night_work
        total_early   += early_work

    wage_per_min   = hourly_wage / 60
    base_pay       = round(total_minutes * wage_per_min)
    night_premium  = round(total_night * wage_per_min * night_rate)
    early_premium  = round(total_early * wage_per_min * early_rate)

    return {
        "work_days":     len(shifts),
        "total_minutes": total_minutes,
        "total_str":     minutes_to_str(total_minutes),
        "night_minutes": total_night,
        "early_minutes": total_early,
        "base_pay":      base_pay,
        "night_premium": night_premium,
        "early_premium": early_premium,
        "salary":        base_pay + night_premium + early_premium,
    }
