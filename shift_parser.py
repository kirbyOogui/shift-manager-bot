"""
shift_parser.py — シフト表・給与明細解析

解析パイプライン（2段階 Vision 構成）:
  Stage 1  OpenCV         : 画像前処理（CLAHE/明るさ補正/ノイズ除去）
  Stage 2  GPT-4o Vision  : 表構造解析・従業員名・日付の読み取り
  Stage 3  GPT-4o Vision  : 1名専用シフト抽出 or 全員バッチ抽出

locate-first: 名前セルの位置を確認してからデータを読む
date-anchor : 日付リストをアンカーとして渡し位置ずれを防ぐ
row_name    : 各エントリに行先頭の名前を含め、Python側で行ずれを検出
unreadable  : 読めないセルは推測せず明示的に返す
adaptive-batch: 日数×人数に応じてバッチサイズを自動調整
ocr-fuzzy   : OCR字形混同（橋↔楠、伶↔鈴、弥↔伽 等）を考慮した名前マッチング

APIコール数:
  特定従業員     : 2回（通常）/ 3回（検証が必要な場合）
  全員・週間表   : 1 + ceil(N/4) 回
  全員・月間表   : 1 + ceil(N/2) 回
  給与明細       : 2回
"""

import base64
import json
import logging
import math
import re
from datetime import datetime
from io import BytesIO

from openai import OpenAI
from PIL import Image, ImageEnhance

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)
logger = logging.getLogger(__name__)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
_TIME_RE            = re.compile(r'^(\d{1,2}):(\d{2})$')
_DATE_RE            = re.compile(r'^\d{4}/\d{2}/\d{2}$')
_RANGE_RE           = re.compile(r'^(\d{1,2}:\d{2})\s*[-~〜～]\s*(\d{1,2}:\d{2})$')
_MAX_CELLS_PER_CALL = 80   # 1コールあたり最大セル数（精度維持の閾値）
_MAX_SHIFT_HOURS    = 18   # これを超えるシフトは疑わしいと判定（時間）


# ─── 前処理: 画像品質向上 ─────────────────────────────────────────────────────

def _preprocess_image(image_bytes: bytes) -> tuple:
    """
    PIL でコントラスト・シャープネス・明るさを最適化して (b64, meta) を返す。

    対策した失敗ケース:
      暗い写真      → 平均輝度が低い場合に明るさを自動補正
      低コントラスト → コントラスト強調（印刷物・スクリーンショット共通）
      ぼけ・手ぶれ  → シャープネス強調（効果は限定的）
      大きすぎる画像 → 長辺 2000px に縮小（detail="high" の最適解像度）
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w0, h0 = img.size

    # 長辺 2000px に制限
    if max(w0, h0) > 2000:
        ratio = 2000 / max(w0, h0)
        img = img.resize((int(w0 * ratio), int(h0 * ratio)), Image.LANCZOS)

    # 平均輝度を PIL ヒストグラムで計算（numpy 不要）
    gray  = img.convert("L")
    hist  = gray.histogram()
    total = sum(hist)
    brightness = sum(i * c for i, c in enumerate(hist)) / max(total, 1)

    # 暗い画像（平均輝度 < 130）は明るさを強調
    if brightness < 130:
        factor = min(170 / max(brightness, 1), 1.7)
        img = ImageEnhance.Brightness(img).enhance(factor)

    img = ImageEnhance.Contrast(img).enhance(1.3)
    img = ImageEnhance.Sharpness(img).enhance(1.5)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64  = base64.b64encode(buf.getvalue()).decode()
    meta = {"original_size": (w0, h0), "final_size": img.size, "brightness": round(brightness, 1)}
    return b64, meta


# ─── OpenCV 前処理 ────────────────────────────────────────────────────────────

def _preprocess_image_cv(image_bytes: bytes) -> tuple:
    """
    OpenCV で高精度な画像前処理を行い (b64, meta) を返す。

    PIL版との違い:
      CLAHE        : 局所コントラスト均等化（印刷ムラ・照明ムラに有効）
      GaussianBlur : カメラノイズを除去してOCR誤読を低減
      長辺2000px   : Vision に最適なサイズ

    Returns:
        b64  : JPEG base64 文字列
        meta : {"size": (w,h), "brightness": float}
    """
    import cv2
    import numpy as np

    nparr = np.frombuffer(image_bytes, np.uint8)
    img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("OpenCV: 画像デコード失敗")

    h, w = img.shape[:2]
    if max(h, w) > 2000:
        ratio = 2000 / max(h, w)
        img   = cv2.resize(img, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LANCZOS4)
        h, w  = img.shape[:2]

    gray       = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < 130:
        factor = min(170.0 / max(brightness, 1.0), 1.7)
        gray   = cv2.convertScaleAbs(gray, alpha=factor, beta=0)

    denoised = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe    = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)

    img_out = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)

    _, buf = cv2.imencode('.jpg', img_out, [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64    = base64.b64encode(buf.tobytes()).decode()

    return b64, {"size": (w, h), "brightness": round(brightness, 1)}


# ─── [削除済み] PP-Structure テーブル抽出 ─────────────────────────────────────
# paddleocr 3.7.0 は fly.io の CPU (x86) で動作不可:
#   PPStructureV3 → RuntimeError: requires "paddlex[ocr]" (未インストール)
#   PaddleOCR.predict() → NotImplementedError: ConvertPirAttribute2RuntimeAttribute
#                         (PaddlePaddle 3.x PIR バックエンドがハードウェア非対応)
# → PaddleOCR を依存から除去し、Vision-only パイプラインを採用。
#
# [Stage2] は常に Vision モードで実行されます。

# ─── OCR字形混同マッチング ────────────────────────────────────────────────────

_OCR_CONFUSION_GROUPS = [
    set('橋楠槁樺梗'),      # 木偏+高さ・形の似た字
    set('伶鈴令零'),        # 令 旁（にんべん/かねへん の違い）
    set('弥彌伽'),          # 弓偏/人偏+伽
    set('浦捕蒲'),          # さんずい/てへん
    set('渡渡波澄'),        # さんずい系
    set('静青晴'),          # 青旁
    set('田由甲申'),        # 田部
    set('己已巳'),          # 横画の本数違い
    set('林本村木'),        # 木偏系
    set('藤籐'),            # くさかんむり系
    set('辺邊邉'),          # 旧字体↔新字体
    set('濱浜'),            # 旧字体↔新字体
    set('斎齋'),            # 旧字体↔新字体
    set('吉𠮷'),            # 土/士の違い（きち/よし）
    set('高髙'),            # 異体字
]


def _ocr_confusable_with(char: str) -> set:
    """OCRで誤認識されやすい文字グループを返す。"""
    for group in _OCR_CONFUSION_GROUPS:
        if char in group:
            return group
    return {char}


def _ocr_fuzzy_score(name1: str, name2: str) -> int:
    """
    OCR字形混同を考慮した文字レベル類似度スコア（0〜min(len1,len2)）。
    長さが異なる場合は0。同じ位置で同一文字または混同グループ内ならカウント。
    """
    if len(name1) != len(name2):
        return 0
    return sum(
        1 for c1, c2 in zip(name1, name2)
        if c1 == c2 or c2 in _ocr_confusable_with(c1)
    )


# ─── [削除済み] PP-Structure内部関数 ─────────────────────────────────────────
# _parse_table_html, _is_good_table, _looks_like_date, _looks_like_name,
# _normalize_date_cell, _detect_doc_type, _analyze_matrix_structure,
# _match_employee_in_matrix, _extract_raw_shifts, _call_interpret_employee_shifts,
# _call_interpret_all_employees, _text_call
# → Vision-only への移行に伴い削除。




# ─── Vision API 共通呼び出し ──────────────────────────────────────────────────

def _vision_call(b64: str, prompt: str, max_tokens: int = 1500) -> str:
    """GPT-4o Vision を呼び出し JSON 文字列を返す。"""
    resp = _client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": "high",
            }},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0,
    )
    return resp.choices[0].message.content or ""


# ─── 時刻・バッチサイズ ───────────────────────────────────────────────────────

def _norm_time(t, *, take_end: bool = False) -> str | None:
    """
    時刻文字列を HH:MM に正規化する。
    - 「9:00〜18:00」範囲形式: take_end=False → 開始, True → 終了
    - 24時超えはそのまま保持（例: 25:30 → "25:30"）
    - "unreadable" / null / 空 → None
    """
    if not t:
        return None
    s = str(t).strip()
    if s in ("", "null", "None", "unreadable", "undefined"):
        return None
    rng = _RANGE_RE.match(s)
    if rng:
        s = rng.group(2) if take_end else rng.group(1)
    m = _TIME_RE.match(s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}"


def _calc_duration_minutes(start: str, end: str) -> int | None:
    """勤務時間（分）を計算する。24時超え・日またぎに対応。"""
    sm = _TIME_RE.match(start)
    em = _TIME_RE.match(end)
    if not (sm and em):
        return None
    sh, smin = int(sm.group(1)), int(sm.group(2))
    eh, emin = int(em.group(1)), int(em.group(2))
    total = (eh * 60 + emin) - (sh * 60 + smin)
    # start > end かつ 24時表記でない場合は日またぎとして +24h
    if total < 0 and sh < 24 and eh < 24:
        total += 24 * 60
    return total


def _calc_batch_size(date_count: int) -> int:
    """
    日数に応じてバッチあたりの従業員数を決定する。
    1コールあたりのセル数 = batch × date_count ≤ _MAX_CELLS_PER_CALL
    上限は 4名/バッチ（これ以上は行ずれリスクが上がる）。
    """
    if date_count <= 0:
        return 4
    return min(max(1, _MAX_CELLS_PER_CALL // date_count), 4)


def _calc_max_tokens_single(date_count: int) -> int:
    """1人分の動的 max_tokens（日数 × 30 + バッファ）。"""
    return max(1500, date_count * 30 + 600)


def _calc_max_tokens_batch(emp_count: int, date_count: int) -> int:
    """バッチ抽出の動的 max_tokens（人数 × 日数 × 30 + バッファ）。"""
    return max(2000, emp_count * date_count * 30 + 800)


# ─── 時刻の正規化ユーティリティ ──────────────────────────────────────────────

def _parse_time_fields(raw_st: str, raw_et: str) -> tuple:
    """
    (raw_start, raw_end) → (st, et) に正規化する。
    範囲形式「9:00〜18:00」も含めて処理する。
    返り値は (st, et) または ("unreadable", "unreadable") または (None, None)。
    """
    raw_st = str(raw_st).strip()
    raw_et = str(raw_et).strip()

    if raw_st == "unreadable" or raw_et == "unreadable":
        return "unreadable", "unreadable"

    # start_time が範囲形式「9:00〜18:00」の場合は分割
    rng = _RANGE_RE.match(raw_st)
    if rng:
        return _norm_time(rng.group(1)), _norm_time(rng.group(2))

    return _norm_time(raw_st), _norm_time(raw_et)


# ─── Call 1: 構造解析 ────────────────────────────────────────────────────────

def _call1_structure(b64: str, today: str, year: str, prev_year: str) -> dict:
    """
    Call 1: ドキュメント種別・表構造・従業員名・日付を解析する。

    データ抽出（時刻の読み取り）は一切行わない。
    「構造を理解すること」と「名前・日付だけを読むこと」に集中させることで精度を上げる。

    Returns:
        document_type : "shift_table" | "payslip" | "unknown"
        orientation   : "row_employee"（行=従業員, 列=日付）
                      | "col_employee"（列=従業員, 行=日付）
        time_format   : "range"（1セルに「9:00-18:00」）
                      | "separate"（出勤・退勤が別セル/別行）
        employees     : ["氏名1", ...]
        dates         : ["YYYY/MM/DD", ...]
        layout_notes  : "特殊なレイアウトに関するメモ"
    """
    prompt = f"""この画像を解析してください。

━━ タスク1: ドキュメント種別 ━━
シフト表・勤務表・出勤表 → "shift_table"
給与明細・賃金明細        → "payslip"
判別不能・その他          → "unknown"

━━ タスク2: 表の向き（シフト表のみ） ━━
行（横）方向に従業員・列（縦）方向に日付 → "row_employee"
列（縦）方向に従業員・行（横）方向に日付 → "col_employee"

━━ タスク3: 時刻の記載形式（シフト表のみ） ━━
「9:00〜18:00」「9:00-18:00」のように1セルに範囲 → "range"
出勤・退勤が別セルまたは上下の別行              → "separate"

━━ タスク4: 従業員名の読み取り（シフト表のみ） ━━
【重要】以下の文字を1文字ずつ丁寧に確認すること:
  ロ(カタカナ) ≠ 口(漢字) ≠ 0(数字)
  ー(カタカナ長音) ≠ 一(漢字) ≠ 1(数字)
  己(き) ≠ 已(い) ≠ 巳(み)
  ヘ(カタカナ) ≠ へ(ひらがな)
【除外】合計・小計・区分・ヘッダー・店長・マネージャー・責任者

━━ タスク5: 日付の読み取り（シフト表のみ） ━━
今日: {today}
・12月のシフト表なら年={prev_year}、1月以降は年={year} として補完
・「1日」「1」「1(月)」など全て YYYY/MM/DD 形式に変換
・月をまたぐ場合は各日付の月を正確に特定する

━━ layout_notes ━━
セル結合・色分けのみ・記号のみ・ジョブカン・KING OF TIME・週表示・月表示など
特殊なレイアウトがあれば記録してください（なければ空文字）

JSONのみ返してください（説明文不要）:
{{
  "document_type": "shift_table",
  "orientation": "row_employee",
  "time_format": "range",
  "employees": ["氏名1", "氏名2"],
  "dates": ["YYYY/MM/DD"],
  "layout_notes": ""
}}"""

    content = _vision_call(b64, prompt, max_tokens=1000)
    try:
        data = json.loads(content)
        data.setdefault("document_type", "unknown")
        data.setdefault("orientation", "row_employee")
        data.setdefault("time_format", "range")
        data.setdefault("layout_notes", "")
        if not isinstance(data.get("employees"), list):
            data["employees"] = []
        if not isinstance(data.get("dates"), list):
            data["dates"] = []
        return data
    except Exception:
        logger.exception("[Call1] JSON解析失敗")
        return {
            "document_type": "unknown",
            "orientation": "row_employee",
            "time_format": "range",
            "employees": [],
            "dates": [],
            "layout_notes": "",
        }


# ─── Call 2A: 1人専用シフト抽出 ──────────────────────────────────────────────

def _call2_single_employee(
    b64: str,
    target_name: str,
    dates: list,
    orientation: str,
    time_format: str,
) -> dict:
    """
    Call 2A: 特定の1人のシフトだけを抽出する（精度最優先）。

    設計の核心:
      locate-first  → 「名前セルの位置を JSON に記録してから読む」を強制
      date-anchor   → 日付リストを渡し「この順番で読む」ことで列ずれを防ぐ
      row_name      → 各エントリに「その行の先頭の名前」を返させ Python 側で行ずれを検出・除外
      unreadable    → 読めないセルは推測せず明示的に返す
    """
    if orientation == "row_employee":
        locate_guide = (
            f"まず「{target_name}」という名前が書かれた行（横1行）を探してください。"
            f"見つけたら「上から何行目」のように location に記録してください。"
            f"見つからない場合は found: false を返してください。"
            f"その行だけを左から右に読んでください。上下の行には絶対に移らないでください。"
        )
        row_name_guide = (
            "各エントリの row_name には「その行の一番左端（名前欄）に書かれている名前」を記録してください。"
            f"row_name は常に「{target_name}」またはそれと同一人物の表記になるはずです。"
            "もし row_name が別の名前になった場合、行がずれているので正しい行に戻ってください。"
        )
    else:
        locate_guide = (
            f"まず「{target_name}」という名前が書かれた列（縦1列）を探してください。"
            f"見つけたら「左から何列目」のように location に記録してください。"
            f"見つからない場合は found: false を返してください。"
            f"その列だけを上から下に読んでください。左右の列には絶対に移らないでください。"
        )
        row_name_guide = (
            "各エントリの row_name には「その列の一番上端（名前欄）に書かれている名前」を記録してください。"
            f"row_name は常に「{target_name}」またはそれと同一人物の表記になるはずです。"
            "もし row_name が別の名前になった場合、列がずれているので正しい列に戻ってください。"
        )

    if time_format == "range":
        time_guide = (
            "時刻は「9:00〜18:00」「9:00-18:00」「09:00-17:30」のような範囲形式です。"
            "「〜」「-」「～」で区切って start_time / end_time に分割してください。"
        )
    else:
        time_guide = (
            "出勤時刻と退勤時刻は別のセルまたは上下の行に分かれています。"
            "出勤時刻 → start_time、退勤時刻 → end_time として読んでください。"
        )

    dates_str = json.dumps(dates, ensure_ascii=False)
    n = len(dates)

    prompt = f"""このシフト表から「{target_name}」さんのシフトだけを読み取ります。

━━ ステップ1: 位置確認 ━━
{locate_guide}

━━ ステップ2: 時刻の読み方 ━━
{time_guide}

━━ ステップ3: 以下の{n}個の日付を順番に読む ━━
{dates_str}

━━ 重要: row_name による自己検証 ━━
{row_name_guide}

━━ 読み取りルール ━━
・時刻は HH:MM 形式（例: 09:00、17:30）
・24時超えはそのまま返す（例: 25:30 → "25:30"、29:00 → "29:00"）
・空欄・「-」・「休」・「公休」・「×」 → シフトなし（結果に含めない）
・「A勤」「夜勤」など時刻ではないコード → shift_code フィールドに記録
・セルに時刻らしきものがあるが判別できない → start_time/end_time を "unreadable" に
・推測禁止: 高確信で読める場合のみ値を返す

━━ 混同しやすい数字（特に注意） ━━
1と7（縦棒の角度）、6と8（上の閉じ方）、0と9（底の形）、3と8（右の閉じ方）

JSONのみ:
{{
  "found": true,
  "location": "{target_name}さんの行/列の位置説明",
  "shifts": [
    {{"date": "YYYY/MM/DD", "row_name": "その行/列の先頭の名前", "start_time": "HH:MM", "end_time": "HH:MM"}},
    {{"date": "YYYY/MM/DD", "row_name": "その行/列の先頭の名前", "start_time": "unreadable", "end_time": "unreadable"}},
    {{"date": "YYYY/MM/DD", "row_name": "その行/列の先頭の名前", "start_time": "09:00", "end_time": "18:00", "shift_code": "A勤"}}
  ]
}}"""

    content = _vision_call(b64, prompt, max_tokens=_calc_max_tokens_single(n))
    try:
        data = json.loads(content)
    except Exception:
        logger.exception(f"[Call2A] {target_name}: JSON解析失敗")
        return {"found": False, "location": "", "shifts": []}

    if not data.get("found", True):
        return {"found": False, "location": data.get("location", ""), "shifts": []}

    shifts = _normalize_shifts(data.get("shifts", []), target_name)
    return {"found": True, "location": data.get("location", ""), "shifts": shifts}


# ─── Call 2B: 複数従業員バッチ抽出 ──────────────────────────────────────────

def _call2_batch_employees(
    b64: str,
    target_names: list,
    dates: list,
    orientation: str,
    time_format: str,
) -> list:
    """
    Call 2B: 複数従業員（最大4名）をまとめて抽出する（全員モード用）。

    1名専用より精度は若干下がるが、全員モードのコスト増を現実的な範囲に抑える。
    各従業員の行/列を個別に特定してから読むよう指示する（locate-first）。
    バッチサイズは _calc_batch_size() で自動調整（日数が多いほど小さくなる）。

    Returns: [{"name", "date", "start_time", "end_time"}, ...]
    """
    names_list = "\n".join(f"  {i+1}. 「{n}」" for i, n in enumerate(target_names))

    if orientation == "row_employee":
        direction = (
            "各従業員の名前が書かれた行（横1行）を1人ずつ個別に特定してから読んでください。"
            "手順: 名前を確認 → その行だけを左から右に読む → 次の人へ。"
            "絶対に行をまたいでデータを混在させないこと。"
        )
    else:
        direction = (
            "各従業員の名前が書かれた列（縦1列）を1人ずつ個別に特定してから読んでください。"
            "手順: 名前を確認 → その列だけを上から下に読む → 次の人へ。"
            "絶対に列をまたいでデータを混在させないこと。"
        )

    if time_format == "range":
        time_guide = "時刻は「9:00〜18:00」形式。start_time / end_time に分割してください。"
    else:
        time_guide = "出勤・退勤は別セル/別行。出勤→start_time、退勤→end_time。"

    dates_str = json.dumps(dates, ensure_ascii=False)
    n = len(dates)

    prompt = f"""このシフト表から以下の従業員のシフトを読み取ってください:
{names_list}

━━ 日付リスト（この{n}個を順番に読む） ━━
{dates_str}

━━ 読み取り方法 ━━
{direction}

━━ 時刻形式 ━━
{time_guide}

━━ ルール ━━
・時刻は HH:MM（24時超えも保持: 25:00 など）
・空欄・「-」・「休」・「公休」 → 含めない
・「A勤」など時刻コード → shift_code に記録
・読めないセル → "unreadable"（推測禁止）
・混同注意: 1↔7、6↔8、0↔9、3↔8

JSONのみ:
{{
  "employees": [
    {{
      "name": "氏名",
      "found": true,
      "shifts": [
        {{"date": "YYYY/MM/DD", "start_time": "HH:MM", "end_time": "HH:MM"}}
      ]
    }}
  ]
}}"""

    content = _vision_call(
        b64, prompt,
        max_tokens=_calc_max_tokens_batch(len(target_names), n),
    )
    try:
        data = json.loads(content)
        employees_data = data.get("employees", [])
    except Exception:
        logger.exception("[Call2B] JSON解析失敗")
        return []

    result = []
    for emp in employees_data:
        name = str(emp.get("name", "")).strip()
        if not name or not emp.get("found", True):
            continue
        for s in emp.get("shifts", []):
            entry = _normalize_batch_entry(name, s)
            if entry:
                result.append(entry)
    return result


def _normalize_batch_entry(name: str, s: dict) -> dict | None:
    """バッチ抽出の1エントリを正規化する。"""
    date = str(s.get("date", "")).strip()
    if not _DATE_RE.match(date):
        return None

    raw_st = str(s.get("start_time", "")).strip()
    raw_et = str(s.get("end_time", "")).strip()
    code   = s.get("shift_code")

    st, et = _parse_time_fields(raw_st, raw_et)

    if st == "unreadable":
        entry = {"name": name, "date": date, "start_time": "unreadable", "end_time": "unreadable"}
    elif st and et:
        entry = {"name": name, "date": date, "start_time": st, "end_time": et}
    else:
        return None

    if code:
        entry["shift_code"] = str(code)
    return entry


def _row_name_matches(row_name: str, target_name: str) -> bool:
    """
    row_name がターゲット名と同一人物かを判定する。
    完全一致・部分一致・共通文字2以上のいずれかで True を返す。
    row_name が空文字の場合は検証スキップ（True を返す）。
    """
    if not row_name:
        return True
    if row_name == target_name:
        return True
    if target_name in row_name or row_name in target_name:
        return True
    # 共通文字数2以上（OCR誤読・略称を考慮）
    return sum(1 for c in target_name if c in row_name) >= 2


def _normalize_shifts(raw: list, target_name: str = "") -> list:
    """
    Call 2A の生の結果を正規化する。

    row_name 自己検証:
      Vision が返した各エントリの row_name（その行/列の先頭の名前）を検証し、
      target_name と一致しない場合は行ずれと判定して除外する。
      row_name が省略されたエントリは除外しない（下位互換）。
    """
    result = []
    for s in raw:
        date = str(s.get("date", "")).strip()
        if not _DATE_RE.match(date):
            continue

        # ── row_name 検証（行ずれ検出） ─────────────────────────────────────
        row_name = str(s.get("row_name", "")).strip()
        if target_name and row_name and not _row_name_matches(row_name, target_name):
            logger.warning(
                f"[row_name] 行ずれ検出・除外: "
                f"target={target_name!r}, row_name={row_name!r}, date={date}"
            )
            continue

        raw_st = str(s.get("start_time", "")).strip()
        raw_et = str(s.get("end_time", "")).strip()
        code   = s.get("shift_code")

        st, et = _parse_time_fields(raw_st, raw_et)

        if st == "unreadable":
            entry = {"date": date, "start_time": "unreadable", "end_time": "unreadable"}
        elif st and et:
            entry = {"date": date, "start_time": st, "end_time": et}
        else:
            continue

        if code:
            entry["shift_code"] = str(code)
        result.append(entry)
    return result


# ─── Call 3: 検証（条件付き） ────────────────────────────────────────────────

def _should_verify(shifts: list) -> bool:
    """
    検証コールが必要かを判断する。

    以下の条件のいずれかに該当する場合に True を返す:
      - シフトが0件（名前は見つかったのに）
      - unreadable が全体の50%以上
    """
    if not shifts:
        return True
    unreadable = sum(1 for s in shifts if s.get("start_time") == "unreadable")
    return (unreadable / len(shifts)) >= 0.5


def _call3_verify(
    b64: str,
    target_name: str,
    dates: list,
    orientation: str,
    time_format: str,
) -> list:
    """
    Call 3: 検証コール（Call 2A の結果が不十分な場合のみ実行）。

    異なるアプローチのプロンプトで再試行する。
    名前の表記ゆれ（漢字↔ひらがな・旧字体など）も考慮させる。
    """
    if orientation == "row_employee":
        search_guide = (
            f"シフト表の左端の列に従業員名が並んでいます。"
            f"上から順に名前を確認し「{target_name}」に最も近い名前の行を探してください。"
            f"見つけたらその行を左から右に読んでください。"
        )
    else:
        search_guide = (
            f"シフト表の最上行に従業員名が並んでいます。"
            f"左から順に名前を確認し「{target_name}」に最も近い名前の列を探してください。"
            f"見つけたらその列を上から下に読んでください。"
        )

    if time_format == "range":
        time_guide = "時刻は「9:00〜18:00」形式。start_time / end_time に分割。"
    else:
        time_guide = "出勤・退勤は別セル/別行。出勤→start_time、退勤→end_time。"

    dates_str = json.dumps(dates, ensure_ascii=False)

    prompt = f"""【再確認】このシフト表で「{target_name}」さんのシフトを読み取ってください。

{search_guide}

日付リスト: {dates_str}

{time_guide}

注意: 名前の表記ゆれがある可能性があります
（例: 漢字↔ひらがな、旧字体↔新字体、フルネーム↔苗字のみ）
「{target_name}」に最も近い表記の行/列を選んでください。

ルール:
・時刻は HH:MM（24時超え保持）
・空欄・休み → 含めない
・読めない → "unreadable"

JSONのみ:
{{
  "found": true,
  "name_in_table": "表での実際の表記",
  "shifts": [
    {{"date": "YYYY/MM/DD", "start_time": "HH:MM", "end_time": "HH:MM"}}
  ]
}}"""

    content = _vision_call(b64, prompt, max_tokens=_calc_max_tokens_single(len(dates)))
    try:
        data = json.loads(content)
        if not data.get("found", True):
            return []
        return _normalize_shifts(data.get("shifts", []), target_name)
    except Exception:
        logger.exception("[Call3] 検証コール失敗")
        return []


# ─── バリデーション ───────────────────────────────────────────────────────────

def _validate_shifts(shifts: list) -> tuple:
    """
    シフトエントリを包括的にバリデーションする。

    チェック項目:
      ① 日付形式 (YYYY/MM/DD)
      ② 時刻形式 (HH:MM)
      ③ シフト時間が 0〜_MAX_SHIFT_HOURS 時間の範囲内か
      ④ 同一 (氏名・日付) の重複除去
      ⑤ unreadable エントリは除外せず保持（後続処理で活用）

    Returns: (valid_shifts, warnings)
    """
    valid    = []
    warnings = []
    seen     = set()

    for s in shifts:
        name  = s.get("name", "")
        date  = s.get("date", "")
        start = s.get("start_time", "")
        end   = s.get("end_time", "")

        if not _DATE_RE.match(date):
            warnings.append(f"日付形式不正: {s}")
            continue

        key = (name, date)
        if key in seen:
            warnings.append(f"重複除外: {name} / {date}")
            continue
        seen.add(key)

        # unreadable は有効エントリとして保持
        if start == "unreadable" or end == "unreadable":
            valid.append(s)
            continue

        if not _TIME_RE.match(start):
            warnings.append(f"start_time形式不正: {s}")
            continue
        if not _TIME_RE.match(end):
            warnings.append(f"end_time形式不正: {s}")
            continue

        duration = _calc_duration_minutes(start, end)
        if duration is not None:
            if duration == 0:
                warnings.append(f"ゼロ長シフト除外: {s}")
                continue
            if duration > _MAX_SHIFT_HOURS * 60:
                warnings.append(f"{_MAX_SHIFT_HOURS}時間超（疑わしい）: {s}")
                continue

        valid.append(s)

    return valid, warnings


# ─── 従業員名マッチング ───────────────────────────────────────────────────────

def _match_employee(employee_name: str, all_employees: list) -> str | None:
    """
    設定済み名前と画像内 OCR 名をマッチングする。

    優先順位:
      1. 完全一致
      2. 部分一致（設定名が OCR 名に含まれる、または逆）
      3. 先頭2文字一致
      4. 共通文字数2以上
      5. OCR字形混同を考慮したファジーマッチング（同長で1文字のみ誤読）
      6. GPT-4o-mini によるファジーマッチング（OCR 誤読を考慮）
    """
    if not employee_name or not all_employees:
        return None

    # ステップ1: 完全一致
    if employee_name in all_employees:
        logger.info(f"[NameMatch] {employee_name!r} → 完全一致")
        return employee_name

    # ステップ2: 部分一致
    for name in all_employees:
        if employee_name in name or name in employee_name:
            logger.info(f"[NameMatch] {employee_name!r} → 部分一致: {name!r}")
            return name

    # ステップ3: 先頭2文字一致
    if len(employee_name) >= 2:
        for name in all_employees:
            if name.startswith(employee_name[:2]):
                logger.info(f"[NameMatch] {employee_name!r} → 先頭2文字一致: {name!r}")
                return name

    # ステップ4: 共通文字数2以上
    for name in all_employees:
        if sum(1 for c in employee_name if c in name) >= 2:
            logger.info(f"[NameMatch] {employee_name!r} → 共通文字≥2: {name!r}")
            return name

    # ステップ5: OCR字形混同を考慮したスコアリング（同長で1文字のみ誤読を許容）
    if len(employee_name) >= 2:
        threshold = max(len(employee_name) - 1, 2)
        for name in all_employees:
            score = _ocr_fuzzy_score(employee_name, name)
            if score >= threshold:
                logger.info(
                    f"[NameMatch] {employee_name!r} → OCR字形混同(score={score}/{len(employee_name)}): {name!r}"
                )
                return name

    # ステップ6: GPT-4o-mini
    logger.info(f"[NameMatch] {employee_name!r} → GPT-miniマッチング試行 (候補={len(all_employees)}名)")
    result = _gpt_match_employee(employee_name, all_employees)
    if result:
        logger.info(f"[NameMatch] {employee_name!r} → GPT-mini結果: {result!r}")
    else:
        logger.warning(
            f"[NameMatch] {employee_name!r} → 全ステップ未発見 "
            f"(OCR検出名リスト: {all_employees})"
        )
    return result


def _gpt_match_employee(employee_name: str, all_employees: list) -> str | None:
    """GPT-4o-mini で OCR 誤読・表記ゆれを考慮したファジーマッチングを行う。"""
    if not all_employees:
        return None
    names_str = "\n".join(f"- {n}" for n in all_employees)
    prompt = (
        f"シフト表の名前リスト（OCR誤読の可能性あり）:\n{names_str}\n\n"
        f"「{employee_name}」と同一人物と思われる名前を1つ選んでください。\n\n"
        f"OCRでは以下のような誤読が頻繁に発生します（積極的に候補を探してください）:\n"
        f"・木偏の文字混同: 橋↔楠↔槁↔樺（木+各部首の形が似た字）\n"
        f"・令旁の文字混同: 伶↔鈴↔令↔零（令を含む字、にんべん/かねへんの違い）\n"
        f"・弓偏/人偏: 弥↔彌↔伽\n"
        f"・さんずい混同: 渡↔波、浦↔捕 など\n"
        f"・青旁: 静↔青↔晴 など\n"
        f"・旧字体↔新字体: 濱↔浜、斎↔齋、辺↔邊 など\n"
        f"・苗字のみ↔フルネームの一致\n\n"
        f"全く一致しそうな候補がない場合のみ null を返してください。\n"
        f'JSONのみ: {{"name": "選んだ名前 または null"}}'
    )
    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=60,
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or "")
        found = data.get("name")
        return found if found and found in all_employees else None
    except Exception:
        logger.exception("[NameMatch] GPT-miniマッチング例外")
        return None


# ─── 給与明細解析 ─────────────────────────────────────────────────────────────

def _parse_payslip(b64: str, year_month: str) -> dict:
    """
    給与明細を2ステップVisionで解析。
    Sub-Call A: レイアウト・支給年月の把握
    Sub-Call B: 各項目の数値抽出
    Retry    : gross_salary が null の場合に総支給額のみ再取得
    """
    # ── Sub-Call A: レイアウト把握 ──────────────────────────────────────────
    logger.info("[Payslip-A] レイアウト解析開始")
    layout_prompt = f"""この給与明細の構造を把握してください。支給年月のデフォルト: {year_month}

以下を特定してください:
1. 支給年月（給与計算対象月 or 支給日の年月）
2. 総支給額・支給合計が記載されているラベル名
3. 控除項目として記載されているラベル名（健康保険・厚生年金・所得税 等）
4. レイアウト形式（表形式・縦リスト・2カラム等）

JSONのみ返してください:
{{
  "year_month": "YYYY/MM",
  "gross_label": "支給合計",
  "deduction_labels": ["健康保険料", "厚生年金保険料"],
  "layout": "table",
  "notes": ""
}}"""

    try:
        layout_content = _vision_call(b64, layout_prompt, max_tokens=500)
        layout = json.loads(layout_content)
        detected_ym = layout.get("year_month") or year_month
        logger.info(
            f"[Payslip-A] 完了: ym={detected_ym}, layout={layout.get('layout')}, "
            f"gross_label={layout.get('gross_label')!r}, "
            f"deduction_labels={layout.get('deduction_labels')}"
        )
    except Exception as e:
        logger.warning(f"[Payslip-A] 失敗({e}) → デフォルトで続行")
        layout = {}
        detected_ym = year_month

    # ── Sub-Call B: 数値抽出 ─────────────────────────────────────────────
    logger.info(f"[Payslip-B] 数値抽出開始 (year_month={detected_ym})")
    extract_prompt = f"""この給与明細から数値を読み取ってください。支給年月: {detected_ym}

【数字の読み取りルール】
・「,」区切りの数字は整数で返す（例: 250,000 → 250000）
・「円」「¥」は除く
・記載がない項目は null（0円の記載がある場合は 0）

支給項目:
- 基本給 → "basic_salary"
- 総支給額・支給合計・支給計 → "gross_salary"（最重要）
- 手当類（基本給以外の支給項目）→ "allowances": [{{"name": "手当名", "amount": 数値}}]

勤怠項目（記載があれば）:
- 有休・有給・有給休暇・年次有給の使用日数 → "paid_leave_days": <数値またはnull>
  （例: 「有休 2日」→ 2.0、「有給休暇 0.5日」→ 0.5）
  ※「0日」または記載なしは null

控除項目:
【標準項目】記載があるものだけ整数で返す（なければ null）
- 健康保険料 → "health_insurance"
- 介護保険料 → "nursing_insurance"
- 厚生年金保険料 → "pension"
- 雇用保険料 → "employment_insurance"
- 源泉所得税・所得税 → "income_tax"
- 住民税 → "resident_tax"

【その他控除】上記以外の控除項目は全て名前付きリストで返す
（例: 子育支援・財形貯蓄・組合費・社員食堂・駐車場・前払い等）
→ "deductions_extra": [{{"name": "項目名", "amount": 数値}}]

JSONのみ返してください:
{{
  "type": "payslip",
  "year_month": "{detected_ym}",
  "gross_salary": <整数>,
  "basic_salary": <整数またはnull>,
  "paid_leave_days": <数値またはnull>,
  "health_insurance": <整数またはnull>,
  "nursing_insurance": <整数またはnull>,
  "pension": <整数またはnull>,
  "employment_insurance": <整数またはnull>,
  "income_tax": <整数またはnull>,
  "resident_tax": <整数またはnull>,
  "deductions_extra": [],
  "allowances": [],
  "note": ""
}}"""

    try:
        content = _vision_call(b64, extract_prompt, max_tokens=1200)
        result = json.loads(content)
        result["type"] = "payslip"
        gross = result.get("gross_salary")
        extra = result.get("deductions_extra") or []
        logger.info(
            f"[Payslip-B] 完了: ym={result.get('year_month')}, gross={gross}, "
            f"basic={result.get('basic_salary')}, health={result.get('health_insurance')}, "
            f"pension={result.get('pension')}, income_tax={result.get('income_tax')}, "
            f"allowances={len(result.get('allowances') or [])}, "
            f"deductions_extra={[(d.get('name'), d.get('amount')) for d in extra]}"
        )
    except Exception as e:
        logger.error(f"[Payslip-B] JSON解析失敗({e})")
        return {"type": "payslip", "year_month": detected_ym, "gross_salary": None}

    # ── Retry: gross_salary が取れなかった場合 ─────────────────────────────
    if result.get("gross_salary") is None:
        logger.warning("[Payslip-Retry] gross_salary=null → 総支給額のみ再取得")
        retry_prompt = (
            "この給与明細の「総支給額」「支給合計」「支給計」「支給額」にあたる"
            "金額を1つだけ読み取ってください。\n"
            "「,」区切りは整数として返してください（例: 250,000 → 250000）。\n"
            'JSONのみ返してください: {"gross_salary": <整数>}'
        )
        try:
            retry_content = _vision_call(b64, retry_prompt, max_tokens=100)
            retry_data = json.loads(retry_content)
            if retry_data.get("gross_salary"):
                result["gross_salary"] = retry_data["gross_salary"]
                logger.info(f"[Payslip-Retry] 成功: gross_salary={result['gross_salary']}")
            else:
                logger.warning("[Payslip-Retry] gross_salary 取得不可")
        except Exception as e:
            logger.warning(f"[Payslip-Retry] 失敗({e})")

    return result


# ─── メインオーケストレーター ─────────────────────────────────────────────────

def parse_image_auto(image_bytes: bytes, employee_name: str = "", ocr_name: str = "") -> dict:
    """
    シフト表・給与明細を Vision パイプラインで解析するメインエントリポイント。

    Stage 1: OpenCV 前処理（CLAHE/明るさ補正/ノイズ除去）→ PIL フォールバック
    Stage 2: PP-Structure は現環境(paddleocr 3.7.0 / fly.io CPU)で動作不可のため無効
             → 常に Vision モードで処理
    Stage 3: GPT-4o Vision で表構造・名前・日付を解析（Call1）
    Stage 4: GPT-4o Vision で特定従業員のシフト抽出（Call2A）/ 全員バッチ（Call2B）
    """
    now        = datetime.now(config.TIMEZONE)
    today      = now.strftime("%Y/%m/%d")
    year       = now.strftime("%Y")
    prev_year  = str(int(year) - 1)
    year_month = now.strftime("%Y/%m")

    # ── Stage 1: OpenCV 前処理 ─────────────────────────────────────────────────
    try:
        b64, cv_meta = _preprocess_image_cv(image_bytes)
        logger.info(
            f"[Stage1] OpenCV前処理完了: size={cv_meta['size']}, "
            f"brightness={cv_meta['brightness']}"
        )
    except Exception as e:
        logger.warning(f"[Stage1] OpenCV失敗({e}) → PIL前処理")
        try:
            b64, img_meta = _preprocess_image(image_bytes)
            logger.info(f"[Stage1] PIL前処理完了: {img_meta}")
        except Exception:
            logger.warning("[Stage1] PIL前処理も失敗 → 生データ使用")
            b64 = base64.b64encode(image_bytes).decode()

    # ── Stage 2: PP-Structure ─────────────────────────────────────────────────
    # paddleocr 3.7.0 は fly.io の CPU (x86) で動作不可:
    #   - PPStructureV3: RuntimeError (paddlex[ocr] が未インストール)
    #   - PaddleOCR.predict(): NotImplementedError (PIR バックエンドがハードウェア非対応)
    # 依存を requirements.txt から除去済み。Vision モードで全処理を実行する。
    logger.info("[Stage2] PP-Structure: 無効 (paddleocr 3.7.0 / fly.io CPU 非対応) → Visionモード")

    call_count = 0

    # ── Stage 3: Call1 Vision 構造解析 ───────────────────────────────────────
    logger.info("[Call1] Vision構造解析開始")
    structure  = _call1_structure(b64, today, year, prev_year)
    call_count += 1

    doc_type  = structure.get("document_type", "unknown")
    employees = structure.get("employees", [])
    dates     = structure.get("dates", [])
    orient    = structure.get("orientation", "row_employee")
    fmt       = structure.get("time_format", "range")
    notes     = structure.get("layout_notes", "")

    logger.info(
        f"[Call1] 完了: doc={doc_type}, orient={orient}, fmt={fmt}, "
        f"従業員{len(employees)}名, 日付{len(dates)}日"
    )
    if employees:
        logger.info(f"[Call1] 検出従業員リスト: {employees}")
    if not dates:
        logger.warning("[Call1] 日付が0件 → シフト抽出不可")

    if doc_type == "payslip":
        return _parse_payslip(b64, year_month)
    if doc_type != "shift_table":
        return {"type": "unknown"}

    # ── Stage 4 / Call 2A: 特定従業員モード ──────────────────────────────────
    if employee_name:
        lookup_name = ocr_name if ocr_name else employee_name
        logger.info(
            f"[NameMatch] 登録名={employee_name!r}, OCR優先名={ocr_name!r}, "
            f"検索候補={len(employees)}名"
        )
        image_name = (
            _match_employee(lookup_name, employees) or
            (None if lookup_name == employee_name else
             _match_employee(employee_name, employees))
        )
        logger.info(f"[NameMatch] 最終結果: {employee_name!r} → {image_name!r}")

        if not image_name:
            logger.info("[NameMatch] 名前未発見 → ユーザー選択フォールバック")
            return {
                "type": "shift",
                "shifts": [],
                "employee_found": False,
                "detected_names": employees,
                "layout_notes": notes,
                "_b64": b64,
                "_structure": structure,
            }

        logger.info(f"[Call2A] Vision 1人専用抽出開始: {image_name!r}")
        result2a   = _call2_single_employee(b64, image_name, dates, orient, fmt)
        call_count += 1
        raw_shifts = result2a.get("shifts", [])
        logger.info(
            f"[Call2A] 完了: {len(raw_shifts)}件, "
            f"found={result2a.get('found')}, location={result2a.get('location')!r}"
        )

        if _should_verify(raw_shifts):
            logger.info(
                f"[Call3] 検証開始: raw={len(raw_shifts)}件, "
                f"unreadable={sum(1 for s in raw_shifts if s.get('start_time')=='unreadable')}件"
            )
            verified   = _call3_verify(b64, image_name, dates, orient, fmt)
            call_count += 1
            logger.info(f"[Call3] 完了: {len(verified)}件")
            if len(verified) > len(raw_shifts):
                raw_shifts = verified

        validated, warnings = _validate_shifts(
            [{**s, "name": image_name} for s in raw_shifts]
        )
        for w in warnings:
            logger.warning(f"[Validate] {w}")
        for s in validated:
            s["name"] = employee_name

        validated.sort(key=lambda x: x.get("date", ""))
        readable = [s for s in validated if s.get("start_time") != "unreadable"]
        logger.info(
            f"[Validate] 確定: {len(validated)}件 (readable={len(readable)}件, "
            f"unreadable={len(validated)-len(readable)}件), calls={call_count}"
        )

        return {
            "type": "shift",
            "shifts": validated,
            "employee_found": bool(readable),
            "all_names": employees,
            "detected_names": employees,
            "ocr_name_identified": image_name if image_name != employee_name else None,
            "layout_notes": notes,
        }

    # ── Stage 4 / Call 2B: 全員モード ────────────────────────────────────────
    batch_size    = _calc_batch_size(len(dates))
    total_batches = math.ceil(len(employees) / batch_size) if employees else 0
    all_shifts    = []
    logger.info(
        f"[Call2B] 全員モード開始: {len(employees)}名, 日数={len(dates)}, "
        f"バッチサイズ={batch_size}, バッチ数={total_batches}"
    )
    for i in range(0, len(employees), batch_size):
        batch = employees[i:i + batch_size]
        bn    = i // batch_size + 1
        logger.info(f"[Call2B] バッチ[{bn}/{total_batches}]: {batch}")
        batch_shifts = _call2_batch_employees(b64, batch, dates, orient, fmt)
        all_shifts.extend(batch_shifts)
        call_count += 1
        logger.info(f"[Call2B] バッチ[{bn}]完了: {len(batch_shifts)}件")

    validated, warnings = _validate_shifts(all_shifts)
    for w in warnings:
        logger.warning(f"[Validate] {w}")

    validated.sort(key=lambda x: (x.get("name", ""), x.get("date", "")))
    all_names = list(dict.fromkeys(s["name"] for s in validated if s.get("name")))
    logger.info(f"[Call2B] 全員完了: {len(validated)}件, {len(all_names)}名, calls={call_count}")

    return {
        "type": "shift",
        "shifts": validated,
        "employee_found": bool(validated),
        "all_names": all_names,
        "detected_names": employees,
        "layout_notes": notes,
    }


# ─── 名前選択後の再解析（公開関数） ─────────────────────────────────────────

def reparse_with_name(
    b64: str,
    structure: dict,
    selected_name: str,
    original_name: str = "",
) -> dict:
    """
    ユーザーが名前を選択した後に指定従業員のシフトを Vision で再解析する。

    Args:
        b64           : OpenCV/PIL 前処理済み base64
        structure     : Call 1 の解析結果（orientation / time_format / dates を含む）
        selected_name : ユーザーが選択した画像内の名前
        original_name : ユーザーの設定名（表示・登録に使用）
    """
    orient = structure.get("orientation", "row_employee")
    fmt    = structure.get("time_format", "range")
    dates  = structure.get("dates", [])

    logger.info(f"[reparse] selected={selected_name!r}, original={original_name!r}")
    logger.info(f"[reparse] Call2A: Vision 1人専用抽出開始")

    result2a   = _call2_single_employee(b64, selected_name, dates, orient, fmt)
    raw_shifts = result2a.get("shifts", [])
    logger.info(
        f"[reparse] Call2A完了: {len(raw_shifts)}件, "
        f"found={result2a.get('found')}, location={result2a.get('location')!r}"
    )

    if _should_verify(raw_shifts):
        logger.info(
            f"[reparse] Call3: 検証開始 (raw={len(raw_shifts)}件, "
            f"unreadable={sum(1 for s in raw_shifts if s.get('start_time')=='unreadable')}件)"
        )
        verified = _call3_verify(b64, selected_name, dates, orient, fmt)
        logger.info(f"[reparse] Call3完了: {len(verified)}件")
        if len(verified) > len(raw_shifts):
            raw_shifts = verified

    display_name = original_name or selected_name
    validated, warnings = _validate_shifts(
        [{**s, "name": selected_name} for s in raw_shifts]
    )
    for w in warnings:
        logger.warning(f"[reparse] {w}")
    for s in validated:
        s["name"] = display_name

    validated.sort(key=lambda x: x.get("date", ""))
    readable = [s for s in validated if s.get("start_time") != "unreadable"]
    logger.info(
        f"[reparse] 確定: {len(validated)}件 (readable={len(readable)}件, "
        f"unreadable={len(validated)-len(readable)}件)"
    )

    return {
        "type": "shift",
        "shifts": validated,
        "employee_found": bool(readable),
        "ocr_name_identified": selected_name if selected_name != display_name else None,
    }


def reparse_all_employees(
    b64: str,
    structure: dict,
) -> list:
    """
    ユーザーが「全員分を表示」を選択した場合に全従業員のシフトを Vision で抽出する。
    """
    employees  = structure.get("employees", [])
    dates      = structure.get("dates", [])
    orient     = structure.get("orientation", "row_employee")
    fmt        = structure.get("time_format", "range")
    batch_size = _calc_batch_size(len(dates))
    all_shifts = []

    total_batches = math.ceil(len(employees) / batch_size) if employees else 0
    logger.info(f"[reparse_all] Vision 全員モード: {len(employees)}名, バッチ数={total_batches}")

    for i in range(0, len(employees), batch_size):
        batch        = employees[i:i + batch_size]
        bn           = i // batch_size + 1
        logger.info(f"[reparse_all] バッチ[{bn}/{total_batches}]: {batch}")
        batch_shifts = _call2_batch_employees(b64, batch, dates, orient, fmt)
        all_shifts.extend(batch_shifts)
        logger.info(f"[reparse_all] バッチ[{bn}]完了: {len(batch_shifts)}件")

    validated, _ = _validate_shifts(all_shifts)
    validated.sort(key=lambda x: (x.get("name", ""), x.get("date", "")))
    logger.info(f"[reparse_all] 完了: {len(validated)}件")
    return validated


# ─── テキストメッセージ解析 ───────────────────────────────────────────────────

_SYSTEM_PROMPT = """あなたはシフト管理アシスタントのメッセージ解析AIです。
ユーザーのメッセージを解析し、以下のJSON形式で意図を返してください。

意図の種類:
- REGISTER_SHIFT: シフト登録（1日分）
- REGISTER_MULTIPLE_SHIFTS: 複数日・同じ時間帯のシフト一括登録
- REGISTER_SHIFTS_BATCH: 複数日・それぞれ異なる時間帯や条件のシフト一括登録
- DELETE_SHIFT: シフト削除
- LIST_SHIFTS: シフト一覧表示
- MONTHLY_SALARY: 今月の給与・勤務時間確認
- UPDATE_SETTING: グローバル設定変更
- CHECK_SETTING: グローバル設定確認
- CREATE_PROFILE: プロファイル作成または更新
- SWITCH_PROFILE: アクティブプロファイルを切り替え
- UPDATE_PROFILE: プロファイルの特定項目を変更
- LIST_PROFILES: プロファイル一覧表示
- DELETE_PROFILE: プロファイル削除
- REGISTER_DEDUCTIONS: 給与明細の控除データ登録
- CREATE_ALLOWANCE: カスタム手当の作成・更新
- LIST_ALLOWANCES: カスタム手当の一覧表示
- DELETE_ALLOWANCE: カスタム手当の削除
- CREATE_CUSTOM_DEDUCTION: カスタム控除の作成・更新
- LIST_CUSTOM_DEDUCTIONS: カスタム控除の一覧表示
- DELETE_CUSTOM_DEDUCTION: カスタム控除の削除
- UNKNOWN: 不明

REGISTER_SHIFTの場合:
{"intent": "REGISTER_SHIFT", "date": "YYYY/MM/DD", "start_time": "HH:MM"|null, "end_time": "HH:MM"|null, "break_minutes": <数値またはnull>, "title": <文字列またはnull>, "color": <文字列またはnull>}
※ start_time/end_timeはメッセージに時刻の指定がない場合nullを返す（プロファイルのデフォルト時刻を使用）。
※ break_minutesはメッセージに休憩時間の指定がある場合のみ数値で返す。「休憩なし」は0。指定がない場合はnullを返す。
※ titleはメッセージに「プロファイル名:日付 時間」のようにコロンで区切って指定されている場合に文字列で返す。指定がない場合はnullを返す。
※ colorはメッセージに色の指定がある場合に日本語の色名（例: "赤", "青", "緑", "黄", "ピンク", "オレンジ", "紫", "グレー", "薄紫", "青緑", "濃緑"）で返す。指定がない場合はnullを返す。

REGISTER_MULTIPLE_SHIFTSの場合（複数日・全て同じ時間帯）:
{"intent": "REGISTER_MULTIPLE_SHIFTS", "dates": ["YYYY/MM/DD", ...], "start_time": "HH:MM", "end_time": "HH:MM", "break_minutes": <数値またはnull>, "title": <文字列またはnull>, "color": <文字列またはnull>}
※ 2つ以上の日付が指定されており、全て同じ時間帯・条件で登録する場合に使用する。
※ title・colorはメッセージに指定がある場合に返す。指定がない場合はnullを返す。

REGISTER_SHIFTS_BATCHの場合（複数日・それぞれ異なる条件）:
{"intent": "REGISTER_SHIFTS_BATCH", "shifts": [{"date": "YYYY/MM/DD", "start_time": "HH:MM", "end_time": "HH:MM", "break_minutes": <数値またはnull>, "title": <文字列またはnull>, "color": <文字列またはnull>}, ...]}
※ 各シフトが異なる時間帯・休憩時間・タイトル・色を持つ場合に使用する。各フィールドは指定がない場合null。

DELETE_SHIFTの場合:
{"intent": "DELETE_SHIFT", "date": "YYYY/MM/DD"}

UPDATE_SHIFTの場合（シフトの時間を変更・修正）:
{"intent": "UPDATE_SHIFT", "date": "YYYY/MM/DD", "start_time": "HH:MM"|null, "end_time": "HH:MM"|null, "break_minutes": <数値またはnull>}

CREATE_PROFILEの場合（プロファイル作成・上書き更新）:
{"intent": "CREATE_PROFILE", "name": "プロファイル名", "calendar_title": <文字列またはnull>, "start_time": "HH:MM"|null, "end_time": "HH:MM"|null, "break_minutes": <数値またはnull>, "color": <文字列またはnull>, "hourly_wage": <数値またはnull>, "leave_hours": <数値またはnull>}

SWITCH_PROFILEの場合:
{"intent": "SWITCH_PROFILE", "name": "プロファイル名"}

UPDATE_PROFILEの場合（プロファイルの特定項目を変更）:
{"intent": "UPDATE_PROFILE", "name": "プロファイル名", "field": "calendar_title"|"start_time"|"end_time"|"break_minutes"|"color"|"hourly_wage"|"cutoff_day"|"payday"|"leave_hours", "value": <新しい値>}

LIST_PROFILESの場合:
{"intent": "LIST_PROFILES"}

DELETE_PROFILEの場合:
{"intent": "DELETE_PROFILE", "name": "プロファイル名"}

CREATE_ALLOWANCEの場合（カスタム手当の作成・更新）:
{"intent": "CREATE_ALLOWANCE", "name": "手当名", "type": "月額固定|日数比例|期間割増|時間単価", "amount": <数値またはnull>, "rate": <数値またはnull>, "start_month": <数値またはnull>, "start_day": <数値またはnull>, "end_month": <数値またはnull>, "end_day": <数値またはnull>, "profile": <文字列またはnull>}

LIST_ALLOWANCESの場合:
{"intent": "LIST_ALLOWANCES"}

DELETE_ALLOWANCEの場合:
{"intent": "DELETE_ALLOWANCE", "name": "手当名"}

CREATE_CUSTOM_DEDUCTIONの場合（カスタム控除の作成・更新。「日数比例」「固定」「定率」のいずれかで毎月自動的に控除額として反映される項目）:
{"intent": "CREATE_CUSTOM_DEDUCTION", "name": "控除名", "type": "固定|日数比例|定率", "amount": <数値またはnull>, "rate": <数値またはnull>, "profile": <文字列またはnull>}
※ amountは「固定」の場合は月額、「日数比例」の場合は出勤1日あたりの金額。
※ rateは「定率」の場合のみ使用し、総支給額に対するパーセンテージ（例: 0.15% → 0.15）。「総支給額の◯%」「総支給額に応じて」等の表現は定率と判断する。
※ 「定率」だがユーザーが%を知らず「今月◯円引かれた」としか言っていない場合は、rateをnullにしてamountに今月引かれた金額を入れる（システム側で今月の総支給額から自動的に%へ換算する）。

LIST_CUSTOM_DEDUCTIONSの場合:
{"intent": "LIST_CUSTOM_DEDUCTIONS"}

DELETE_CUSTOM_DEDUCTIONの場合:
{"intent": "DELETE_CUSTOM_DEDUCTION", "name": "控除名"}

REGISTER_DEDUCTIONSの場合（給与明細の控除データ登録）:
{"intent": "REGISTER_DEDUCTIONS", "year_month": "YYYY/MM", "gross_salary": <数値>, "health_insurance": <数値またはnull>, "nursing_insurance": <数値またはnull>, "pension": <数値またはnull>, "employment_insurance": <数値またはnull>, "income_tax": <数値またはnull>, "resident_tax": <数値またはnull>, "other": <数値またはnull>}

DELETE_DEDUCTIONSの場合（給与明細データの削除）:
{"intent": "DELETE_DEDUCTIONS", "year_month": "YYYY/MM"}

MODIFY_DEDUCTIONSの場合（給与明細の特定項目のみ変更）:
{"intent": "MODIFY_DEDUCTIONS", "year_month": "YYYY/MM", "field": "gross_salary"|"health_insurance"|"nursing_insurance"|"pension"|"employment_insurance"|"income_tax"|"resident_tax"|"other", "value": <数値>}

UPDATE_SETTINGの場合（グローバル設定変更）:
{"intent": "UPDATE_SETTING", "setting_type": "hourly_wage"|"break_minutes"|"notify_time"|"notify_enabled"|"calendar_title"|"calendar_color"|"social_insurance"|"night_rate"|"early_rate"|"early_end"|"employee_name"|"leave_hours", "value": <数値または文字列またはnull>}
※ 「名前を設定したい」のように変更したい設定の種類だけを述べていて具体的な値（実際の名前・数値など）を
一切含んでいない場合は、value を絶対に推測せず null にすること。

GRANT_LEAVEの場合（有給の付与を記録）:
{"intent": "GRANT_LEAVE", "type": "年次有給|振替休日|特別休暇", "days": <数値>, "granted_date": "YYYY/MM/DD"|null, "expiry_date": "YYYY/MM/DD"|null, "note": <文字列またはnull>}

USE_LEAVEの場合（有給を使用・取得）:
{"intent": "USE_LEAVE", "date": "YYYY/MM/DD", "type": "年次有給|振替休日|特別休暇", "days": <1.0または0.5>, "start_time": "HH:MM"|null, "end_time": "HH:MM"|null}

CHECK_LEAVEの場合（有給残日数を確認）:
{"intent": "CHECK_LEAVE"}

DELETE_LEAVEの場合（有給取得を取り消し・削除）:
{"intent": "DELETE_LEAVE", "date": "YYYY/MM/DD"}

MODIFY_LEAVEの場合（有給取得内容を変更）:
{"intent": "MODIFY_LEAVE", "date": "YYYY/MM/DD", "days": <数値またはnull>, "type": <文字列またはnull>}

DELETE_ALL_DATAの場合（ユーザーの全データを削除）:
{"intent": "DELETE_ALL_DATA"}

CONNECT_CALENDARの場合（GoogleカレンダーのOAuth連携開始）:
{"intent": "CONNECT_CALENDAR"}

DISCONNECT_CALENDARの場合（Googleカレンダーの連携解除）:
{"intent": "DISCONNECT_CALENDAR"}

HELPの場合（使い方・コマンド一覧の表示）:
{"intent": "HELP"}

LIST_SHIFTSの場合（シフト一覧表示）:
{"intent": "LIST_SHIFTS", "year_month": "YYYY/MM"|null, "profile_name": <文字列またはnull>}

MONTHLY_SALARYの場合（給与・勤務時間確認）:
{"intent": "MONTHLY_SALARY", "year_month": "YYYY/MM"|null, "profile_name": <文字列またはnull>}

CHECK_SETTINGの場合:
{"intent": "CHECK_SETTING"}

UNKNOWNの場合:
{"intent": "UNKNOWN"}

━━ 判断に迷いやすい例（重要） ━━
・「8月30日」「8/30」「8月30」のように、日付だけが単独で送られてきた場合は、
  一覧表示の要望ではなく REGISTER_SHIFT（start_time/end_timeはnull）と判断してください。
  日付の数字（1〜31のどれであっても）によって判断を変えないでください。
・LIST_SHIFTSと判断するのは「一覧」「シフト確認」「今月の予定」のように、
  複数日をまとめて確認したい意図がメッセージ中に明示されている場合のみです。

今日の日付: {today}
相対的な日付（来週月曜日など）は今日の日付を基準に絶対日付（YYYY/MM/DD）に変換してください。
"""


def parse_message(text: str) -> dict:
    today  = datetime.now(config.TIMEZONE).strftime("%Y/%m/%d (%A)")
    prompt = _SYSTEM_PROMPT.replace("{today}", today)

    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user",   "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)
