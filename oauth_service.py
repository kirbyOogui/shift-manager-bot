import secrets
from datetime import datetime
from google_auth_oauthlib.flow import Flow
import config
import sheets_service
import line_service

# {state_token: {"user_id": str, "expires_at": float}} - 有効期限付きのOAuthセッション管理
_oauth_states: dict = {}


def _cleanup_oauth_states() -> None:
    """期限切れのOAuthセッションを削除してメモリリークを防ぐ。"""
    now = datetime.now().timestamp()
    expired = [k for k, v in _oauth_states.items() if v["expires_at"] < now]
    for k in expired:
        del _oauth_states[k]

_CLIENT_CONFIG = {
    "web": {
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [config.OAUTH_REDIRECT_URI],
    }
}


def _create_flow(state: str = None) -> Flow:
    kwargs = {"state": state} if state else {}
    return Flow.from_client_config(
        _CLIENT_CONFIG,
        scopes=config.GOOGLE_OAUTH_SCOPES,
        redirect_uri=config.OAUTH_REDIRECT_URI,
        **kwargs,
    )


def generate_auth_url(user_id: str) -> str:
    """LINE UserIDに紐づいたGoogle OAuth認証URLを生成する。"""
    _cleanup_oauth_states()
    flow = _create_flow()
    state = secrets.token_urlsafe(16)
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        prompt="consent",  # 毎回refresh_tokenを取得するために必要
    )
    _oauth_states[state] = {
        "user_id": user_id,
        "code_verifier": flow.code_verifier,  # PKCE: コールバック時のトークン交換に必要
        "expires_at": datetime.now().timestamp() + 600,  # 10分有効
    }
    return auth_url


def handle_callback(state: str, code: str) -> str | None:
    """OAuthコールバックを処理してトークンを保存し、LINE UserIDを返す。失敗時はNoneを返す。"""
    state_data = _oauth_states.pop(state, None)
    if not state_data:
        return None

    if datetime.now().timestamp() > state_data["expires_at"]:
        return None

    user_id = state_data["user_id"]

    flow = _create_flow(state=state)
    flow.code_verifier = state_data.get("code_verifier")
    flow.fetch_token(code=code)
    creds = flow.credentials

    expiry_str = creds.expiry.isoformat() if creds.expiry else ""
    sheets_service.save_user_tokens(
        user_id=user_id,
        access_token=creds.token,
        refresh_token=creds.refresh_token or "",
        token_expiry=expiry_str,
    )

    return user_id
