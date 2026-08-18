import logging
import requests
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    QuickReply,
    QuickReplyItem,
    PostbackAction,
    MessageAction,
)
import config

logger = logging.getLogger(__name__)

_api: MessagingApi | None = None


def _get_api() -> MessagingApi:
    global _api
    if _api is None:
        configuration = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
        _api = MessagingApi(ApiClient(configuration))
    return _api


def reply_text(reply_token: str, text: str) -> None:
    logger.info(f"[reply_text] token={reply_token[:10]}... text={text[:30]}")
    try:
        _get_api().reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)],
        ))
        logger.info("[reply_text] 送信成功")
    except Exception as e:
        logger.error(f"[reply_text] 送信失敗: {e}")
        raise


def reply_with_quickreply(reply_token: str, text: str, items: list[dict]) -> None:
    """Quick Replyボタン付きメッセージを返信する。
    items: [{"label": "ボタンテキスト", "data": "postbackデータ文字列"}]
    """
    logger.info(f"[reply_with_quickreply] token={reply_token[:10]}... text={text[:30]}")
    quick_reply_items = [
        QuickReplyItem(action=PostbackAction(label=item["label"], data=item["data"]))
        for item in items
    ]
    try:
        _get_api().reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(
                text=text,
                quick_reply=QuickReply(items=quick_reply_items),
            )],
        ))
        logger.info("[reply_with_quickreply] 送信成功")
    except Exception as e:
        logger.error(f"[reply_with_quickreply] 送信失敗: {e}")
        raise


def push_text(user_id: str, text: str) -> None:
    logger.info(f"[push_text] to={text[:30]}")
    _get_api().push_message(PushMessageRequest(
        to=user_id,
        messages=[TextMessage(text=text)],
    ))


def reply_flex(reply_token: str, alt_text: str, contents: dict) -> None:
    """Flex Messageを返信する。SDK の dict シリアライズ問題を避けるため requests を使用。"""
    logger.info(f"[reply_flex] token={reply_token[:10]}... alt={alt_text}")
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "flex", "altText": alt_text, "contents": contents}],
    }
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    if r.status_code != 200:
        logger.error(f"[reply_flex] 送信失敗: {r.status_code} {r.text}")
        raise Exception(f"LINE API error {r.status_code}: {r.text}")
    logger.info("[reply_flex] 送信成功")


def push_flex(user_id: str, alt_text: str, contents: dict) -> None:
    """Flex Messageをプッシュ送信する。SDK の dict シリアライズ問題を避けるため requests を使用。"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "flex", "altText": alt_text, "contents": contents}],
    }
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    if r.status_code != 200:
        logger.error(f"[push_flex] 送信失敗: {r.status_code} {r.text}")
        raise Exception(f"LINE API error {r.status_code}: {r.text}")


def send_loading(user_id: str, seconds: int = 20) -> None:
    """チャット画面にローディングアニメーション（タイピングインジケーター）を表示する。
    処理開始時に呼び出すことでユーザーに待機を促す。最大60秒。"""
    url = "https://api.line.me/v2/bot/chat/loading/start"
    headers = {
        "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"chatId": user_id, "loadingSeconds": min(max(seconds, 5), 60)}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception:
        pass  # ローディングAPIの失敗は処理に影響させない


def push_with_quickreply(user_id: str, text: str, items: list[dict]) -> None:
    """Quick Replyボタン付きメッセージをプッシュ送信する。"""
    quick_reply_items = [
        QuickReplyItem(action=PostbackAction(label=item["label"], data=item["data"]))
        for item in items
    ]
    _get_api().push_message(PushMessageRequest(
        to=user_id,
        messages=[TextMessage(
            text=text,
            quick_reply=QuickReply(items=quick_reply_items),
        )],
    ))


def reply_with_message_quickreply(reply_token: str, text: str, items: list) -> None:
    """メッセージ型Quick Replyボタン付きメッセージを返信する。
    items: [("ラベル", "送信テキスト"), ...]  または
           [{"label": "ラベル", "data": "postback_data"}, ...]  の混在可。
    """
    logger.info(f"[reply_with_message_quickreply] token={reply_token[:10]}... text={text[:30]}")
    quick_reply_items = []
    for item in items:
        if isinstance(item, dict):
            quick_reply_items.append(
                QuickReplyItem(action=PostbackAction(label=item["label"], data=item["data"]))
            )
        else:
            label, msg_text = item
            quick_reply_items.append(
                QuickReplyItem(action=MessageAction(label=label, text=msg_text))
            )
    try:
        _get_api().reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(
                text=text,
                quick_reply=QuickReply(items=quick_reply_items),
            )],
        ))
        logger.info("[reply_with_message_quickreply] 送信成功")
    except Exception as e:
        logger.error(f"[reply_with_message_quickreply] 送信失敗: {e}")
        raise


def get_image_content(message_id: str) -> bytes:
    """LINE Content APIから画像バイナリを取得する。"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content
