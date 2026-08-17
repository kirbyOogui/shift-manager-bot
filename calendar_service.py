from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import config
import sheets_service


def _get_credentials(user_id: str) -> Credentials:
    """ユーザーのOAuth認証情報を取得し、期限切れの場合はリフレッシュする。"""
    tokens = sheets_service.get_user_tokens(user_id)
    if not tokens or not tokens.get("refresh_token"):
        raise ValueError("Googleカレンダーが未連携です")

    expiry = None
    expiry_raw = tokens.get("token_expiry")
    if expiry_raw:
        try:
            expiry = datetime.fromisoformat(expiry_raw)
        except ValueError:
            expiry = None

    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=config.GOOGLE_OAUTH_SCOPES,
        expiry=expiry,
    )

    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        expiry_str = creds.expiry.isoformat() if creds.expiry else ""
        sheets_service.save_user_tokens(
            user_id=user_id,
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            token_expiry=expiry_str,
        )

    return creds


def _get_service(user_id: str):
    return build("calendar", "v3", credentials=_get_credentials(user_id))


def create_event(user_id: str, date_str: str, start_time: str, end_time: str, summary: str = "シフト", color_id: str = "") -> str:
    """ユーザーのGoogleカレンダーにイベントを登録し、EventIDを返す。"""
    service = _get_service(user_id)

    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y/%m/%d %H:%M")
    end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y/%m/%d %H:%M")

    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    event = {
        "summary": summary,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
    }
    if color_id:
        event["colorId"] = color_id

    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("id", "")


def create_allday_event(user_id: str, date_str: str, summary: str = "有給", color_id: str = "") -> str:
    """ユーザーのGoogleカレンダーに終日イベントを登録し、EventIDを返す。"""
    service = _get_service(user_id)
    d = datetime.strptime(date_str, "%Y/%m/%d")
    date_iso = d.strftime("%Y-%m-%d")
    next_date_iso = (d + timedelta(days=1)).strftime("%Y-%m-%d")

    event = {
        "summary": summary,
        "start": {"date": date_iso},
        "end":   {"date": next_date_iso},
    }
    if color_id:
        event["colorId"] = color_id

    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("id", "")


def delete_event(user_id: str, event_id: str) -> None:
    """ユーザーのGoogleカレンダーからイベントを削除する。"""
    if not event_id:
        return
    try:
        service = _get_service(user_id)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except Exception:
        pass


def patch_event_color(user_id: str, event_id: str, color_id: str) -> bool:
    """イベントの色だけを変更する。color_id が空文字ならデフォルト色にリセット。成功時 True。"""
    if not event_id:
        return False
    try:
        service = _get_service(user_id)
        service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={"colorId": color_id},
        ).execute()
        return True
    except Exception:
        return False


def update_event(user_id: str, event_id: str, date_str: str, start_time: str, end_time: str, summary: str = "シフト", color_id: str = "") -> None:
    """ユーザーのGoogleカレンダーのイベントを更新する。"""
    if not event_id:
        return
    service = _get_service(user_id)

    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y/%m/%d %H:%M")
    end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y/%m/%d %H:%M")
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    body = {
        "summary": summary,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tokyo"},
    }
    if color_id:
        body["colorId"] = color_id

    service.events().patch(calendarId="primary", eventId=event_id, body=body).execute()
