"""
LINEリッチメニューを作成してデフォルトに設定するスクリプト。
ローカルで一度だけ実行する:
    python setup_richmenu.py
"""

import io
import math
import requests
from PIL import Image, ImageDraw, ImageFont
import config

LINE_API      = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"
HEADERS       = {"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"}

IMG_W, IMG_H = 2500, 1686
COLS, ROWS   = 3, 2
CW = IMG_W // COLS   # 833px
CH = IMG_H // ROWS   # 843px

BUTTONS = [
    {"label": "シフト管理",   "color": (149, 186, 220), "action": "シフト管理"},
    {"label": "給与・明細",   "color": (218, 168, 122), "action": "給与・明細"},   # くすみオレンジ
    {"label": "有給管理",     "color": (130, 205, 218), "action": "有給管理"},    # くすみ水色
    {"label": "仕事名",       "color": (190, 158, 210), "action": "仕事名"},
    {"label": "設定確認",     "color": (163, 174, 180), "action": "現在の設定を確認"},
    {"label": "ヘルプ",       "color": (165, 172, 215), "action": "ヘルプ"},
]

JP_BOLD_FONT_PATHS = [
    "C:/Windows/Fonts/yugothb.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]
WESTERN_BOLD_FONT_PATHS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _center_text(draw, text, font, cx, cy, fill):
    """テキストを (cx, cy) 中心に描画する。"""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        cx_pos = cx - (bbox[0] + bbox[2]) // 2
        cy_pos = cy - (bbox[1] + bbox[3]) // 2
        draw.text((cx_pos, cy_pos), text, font=font, fill=fill)
    except Exception:
        draw.text((cx, cy), text, font=font, fill=fill)


def draw_icon(draw, label: str, cx: int, cy: int, size: int) -> None:
    """全アイコンを統一フラットデザイン（白・同一ストローク幅）で描画する。"""
    h = size // 2
    W = max(14, size // 18)   # 全アイコン共通のストローク幅
    C = (255, 255, 255)        # 白

    if label == "シフト管理":
        # カレンダーアイコン：角丸四角 + 綴じリング2つ + ヘッダー線 + 行3本
        bw, bh = int(h * 1.05), int(h * 1.25)
        rr = W * 2
        draw.rounded_rectangle(
            [cx - bw//2, cy - bh//2, cx + bw//2, cy + bh//2],
            radius=rr, outline=C, width=W
        )
        # 綴じリング（上端をまたぐ縦長の角丸四角）
        rw, rh = int(bw * 0.13), int(bh * 0.18)
        for rx in [-bw//4, bw//4]:
            draw.rounded_rectangle(
                [cx + rx - rw//2, cy - bh//2 - rh//2,
                 cx + rx + rw//2, cy - bh//2 + rh//2],
                radius=W, fill=C
            )
        # ヘッダー区切り線
        header_y = cy - bh//2 + int(bh * 0.28)
        draw.line(
            [cx - bw//2 + W, header_y, cx + bw//2 - W, header_y],
            fill=C, width=max(W - 4, 8)
        )
        # コンテンツ行3本
        lx0 = cx - bw//2 + W * 2
        lx1 = cx + bw//2 - W * 2
        for fy in [0.45, 0.65, 0.83]:
            ly = int(cy - bh//2 + bh * fy)
            draw.line([lx0, ly, lx1, ly], fill=C, width=max(W - 8, 5))

    elif label == "給与・明細":
        # 円 + ¥マーク
        r = int(h * 0.83)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=C, width=W)
        fnt = _load_font(WESTERN_BOLD_FONT_PATHS, int(size * 0.46))
        _center_text(draw, "¥", fnt, cx, cy, C)

    elif label == "有給管理":
        # 太陽（有給＝休日）：円＋8本の放射線
        sun_r     = int(h * 0.42)
        ray_inner = int(h * 0.54)
        ray_outer = int(h * 0.78)
        draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r],
                     outline=C, width=W)
        lw_ray = max(W - 6, 6)
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            x1 = cx + ray_inner * math.cos(rad)
            y1 = cy + ray_inner * math.sin(rad)
            x2 = cx + ray_outer * math.cos(rad)
            y2 = cy + ray_outer * math.sin(rad)
            draw.line([x1, y1, x2, y2], fill=C, width=lw_ray)

    elif label == "仕事名":
        # 塗りつぶしシルエット：頭（円）＋体（楕円ドーム）
        head_r  = int(h * 0.30)          # 84px
        head_cy = cy - int(h * 0.28)     # cy - 78
        draw.ellipse(
            [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
            fill=C
        )
        # 肩・胸：頭より少し広めのコンパクトなドーム
        body_w = int(h * 0.64)           # 179px（頭径の約2倍）
        body_h = int(h * 0.48)           # 134px
        body_cy = cy + int(h * 0.54)     # cy + 151
        draw.chord(
            [cx - body_w, body_cy - body_h, cx + body_w, body_cy + body_h],
            180, 360, fill=C
        )

    elif label == "設定確認":
        # イコライザースライダー3本：水平線＋ノブ円
        lx0 = cx - int(h * 0.72)
        lx1 = cx + int(h * 0.72)
        lw_s = max(W - 4, 8)
        kr   = W + 6   # ノブの半径
        for fy, knob_rel in [(-0.38, 0.28), (0.0, -0.22), (0.38, 0.45)]:
            ly = int(cy + h * fy)
            draw.line([lx0, ly, lx1, ly], fill=C, width=lw_s)
            kx = cx + int(h * knob_rel)
            draw.ellipse([kx - kr, ly - kr, kx + kr, ly + kr], fill=C)

    elif label == "ヘルプ":
        # 丸の中に ? （インフォボタン風）
        circ_r = int(h * 0.83)
        draw.ellipse([cx - circ_r, cy - circ_r, cx + circ_r, cy + circ_r],
                     outline=C, width=W)
        fnt = _load_font(WESTERN_BOLD_FONT_PATHS, int(size * 0.46))
        _center_text(draw, "?", fnt, cx, cy, C)


def create_image() -> bytes:
    label_font = _load_font(JP_BOLD_FONT_PATHS, 90)

    img  = Image.new("RGB", (IMG_W, IMG_H), (248, 249, 250))
    draw = ImageDraw.Draw(img)

    ICON_SIZE = 532   # 560 × 0.95 = 5%縮小
    RADIUS    = 106   # ICON_SIZE × 0.20
    TOP_PAD   = 70
    LABEL_GAP = 46    # アイコン下端〜ラベル間を広げる

    for idx, btn in enumerate(BUTTONS):
        row = idx // COLS
        col = idx % COLS
        x0, y0 = col * CW, row * CH
        cx = x0 + CW // 2

        draw.rectangle([x0, y0, x0 + CW - 1, y0 + CH - 1], fill=(255, 255, 255))

        ix = cx - ICON_SIZE // 2
        iy = y0 + TOP_PAD
        draw.rounded_rectangle(
            [ix, iy, ix + ICON_SIZE, iy + ICON_SIZE],
            radius=RADIUS,
            fill=btn["color"],
        )

        icon_cx = cx
        icon_cy = iy + ICON_SIZE // 2
        draw_icon(draw, btn["label"], icon_cx, icon_cy, ICON_SIZE)

        label_cy = iy + ICON_SIZE + LABEL_GAP + 27
        _center_text(draw, btn["label"], label_font, cx, label_cy, fill=(33, 33, 33))

    DIV = (224, 224, 224)
    for c in range(1, COLS):
        draw.line([(c * CW, 0), (c * CW, IMG_H)], fill=DIV, width=2)
    for r in range(1, ROWS):
        draw.line([(0, r * CH), (IMG_W, r * CH)], fill=DIV, width=2)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def delete_existing() -> None:
    r = requests.get(f"{LINE_API}/richmenu/list", headers=HEADERS)
    if not r.ok:
        return
    for menu in r.json().get("richmenus", []):
        requests.delete(f"{LINE_API}/richmenu/{menu['richMenuId']}", headers=HEADERS)
        print(f"  削除: {menu['richMenuId']}")


def create_rich_menu() -> str:
    areas = []
    for idx, btn in enumerate(BUTTONS):
        row = idx // COLS
        col = idx % COLS
        areas.append({
            "bounds": {"x": col * CW, "y": row * CH, "width": CW, "height": CH},
            "action": {"type": "message", "text": btn["action"]},
        })

    body = {
        "size": {"width": IMG_W, "height": IMG_H},
        "selected": True,
        "name": "シフト管理メニュー",
        "chatBarText": "メニュー",
        "areas": areas,
    }
    r = requests.post(
        f"{LINE_API}/richmenu",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=body,
    )
    r.raise_for_status()
    return r.json()["richMenuId"]


def upload_image(menu_id: str, image_bytes: bytes) -> None:
    r = requests.post(
        f"{LINE_DATA_API}/richmenu/{menu_id}/content",
        headers={**HEADERS, "Content-Type": "image/jpeg"},
        data=image_bytes,
    )
    r.raise_for_status()


def set_default(menu_id: str) -> None:
    r = requests.post(f"{LINE_API}/user/all/richmenu/{menu_id}", headers=HEADERS)
    r.raise_for_status()


if __name__ == "__main__":
    print("【既存リッチメニューを削除】")
    delete_existing()

    print("【画像を生成中...】")
    image_bytes = create_image()
    print(f"  画像サイズ: {len(image_bytes):,} bytes")

    print("【リッチメニューを作成中...】")
    menu_id = create_rich_menu()
    print(f"  作成ID: {menu_id}")

    print("【画像をアップロード中...】")
    upload_image(menu_id, image_bytes)

    print("【デフォルトに設定中...】")
    set_default(menu_id)

    print("\n[完了] LINEを開くとメニューが表示されます。")
