import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()  # ローカル開発時に .env を読み込む（本番環境では無視される）

# LINE
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Google Sheets（サービスアカウントJSONを文字列で設定）
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")

# Google OAuth（各ユーザーのカレンダー連携用）
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080")
OAUTH_REDIRECT_URI = f"{APP_BASE_URL}/oauth/callback"
GOOGLE_OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# タイムゾーン
TIMEZONE = ZoneInfo("Asia/Tokyo")

# ユーザーデフォルト設定
DEFAULT_HOURLY_WAGE = 1000
DEFAULT_BREAK_MINUTES = 60
DEFAULT_NOTIFY_TIME = "20:00"
DEFAULT_NOTIFY_ENABLED = "ON"
DEFAULT_CALENDAR_TITLE = "シフト"
DEFAULT_NIGHT_RATE = 25   # 深夜割増率（%）
DEFAULT_EARLY_RATE = 0    # 早朝割増率（%）
DEFAULT_EARLY_END  = "08:00"  # 早朝手当終了時刻
SHEET_PROFILES = "シフトプロファイル"
DEFAULT_CALENDAR_COLOR = ""

# Google Calendar カラーID対応表
CALENDAR_COLOR_MAP = {
    "ラベンダー": "1", "薄紫": "1",
    "セージ": "2", "セージグリーン": "2",
    "グレープ": "3", "紫": "3",
    "フラミンゴ": "4", "ピンク": "4",
    "バナナ": "5", "黄": "5", "黄色": "5",
    "タンジェリン": "6", "オレンジ": "6", "ミカン": "6",
    "ピーコック": "7", "青緑": "7", "水色": "7",
    "グラファイト": "8", "グレー": "8", "灰": "8",
    "ブルーベリー": "9", "青": "9",
    "バジル": "10", "濃緑": "10", "緑": "10",
    "トマト": "11", "赤": "11",
}

# Google Sheetsシート名
SHEET_SHIFTS = "シフトデータ"
SHEET_SETTINGS = "ユーザー設定"
SHEET_DEDUCTIONS = "控除データ"
SHEET_ALLOWANCES = "カスタム手当"
SHEET_CUSTOM_DEDUCTIONS = "カスタム控除"
SHEET_LEAVE = "有給管理"
