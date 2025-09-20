import io
from typing import Optional

import streamlit as st
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_oauth_client_config() -> dict:
    if "google_oauth" not in st.secrets:
        raise ValueError("OAuth クライアント情報が secrets.toml に設定されていません")

    oauth_conf = st.secrets["google_oauth"]
    required_keys = ("client_id", "client_secret", "project_id")
    missing = [key for key in required_keys if key not in oauth_conf]
    if missing:
        raise ValueError(f"OAuth クライアント情報が不足しています: {', '.join(missing)}")

    redirect_uris = oauth_conf.get("redirect_uris")
    if not redirect_uris:
        redirect_uri = oauth_conf.get("redirect_uri", "urn:ietf:wg:oauth:2.0:oob")
        redirect_uris = [redirect_uri]
    elif isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]

    return {
        "installed": {
            "client_id": oauth_conf["client_id"],
            "project_id": oauth_conf["project_id"],
            "auth_uri": oauth_conf.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": oauth_conf.get("token_uri", "https://oauth2.googleapis.com/token"),
            "auth_provider_x509_cert_url": oauth_conf.get(
                "auth_provider_x509_cert_url",
                "https://www.googleapis.com/oauth2/v1/certs",
            ),
            "client_secret": oauth_conf["client_secret"],
            "redirect_uris": redirect_uris,
        }
    }


def ensure_drive_credentials():
    creds = st.session_state.get("drive_credentials")
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return st.session_state.get("drive_credentials")


def build_drive_service():
    creds = ensure_drive_credentials()
    if not creds:
        raise ValueError("認証が完了していません")
    return build("drive", "v3", credentials=creds)


def upload_file_to_drive(uploaded_file, folder_id: Optional[str] = None) -> dict:
    """Upload an in-memory Streamlit file to Google Drive and return its metadata."""
    service = build_drive_service()
    body = {"name": uploaded_file.name}
    if folder_id:
        body["parents"] = [folder_id]

    file_bytes = uploaded_file.getvalue()
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=uploaded_file.type or "application/octet-stream",
        resumable=False,
    )

    return (
        service.files()
        .create(body=body, media_body=media, fields="id, name, webViewLink")
        .execute()
    )

st.set_page_config(page_title="Chat Demo", page_icon="💬", layout="wide")

st.title("Chat Demo")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "こんにちは！ご質問はありますか？"}
    ]

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("メッセージを入力してください"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    response = "すみません、まだ応答ロジックを実装していません。"
    st.session_state.messages.append({"role": "assistant", "content": response})
    with st.chat_message("assistant"):
        st.markdown(response)

st.subheader("Google Drive アップロード")

if "drive_credentials" not in st.session_state:
    st.session_state.drive_credentials = None

if "drive_auth_flow" not in st.session_state:
    st.session_state.drive_auth_flow = None

if "drive_auth_url" not in st.session_state:
    st.session_state.drive_auth_url = None


def start_oauth_flow():
    client_config = _get_oauth_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, DRIVE_SCOPES)
    redirect_uris = client_config["installed"].get("redirect_uris") or ["urn:ietf:wg:oauth:2.0:oob"]
    flow.redirect_uri = redirect_uris[0]
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
    st.session_state.drive_auth_flow = flow
    st.session_state.drive_auth_url = auth_url


def complete_oauth_flow(auth_code: str):
    flow = st.session_state.get("drive_auth_flow")
    if not flow:
        st.error("セッションが切れました。最初からやり直してください。")
        return
    try:
        flow.fetch_token(code=auth_code)
    except Exception as exc:  # noqa: BLE001
        st.error(f"認証に失敗しました: {exc}")
    else:
        st.session_state.drive_credentials = flow.credentials
        st.session_state.drive_auth_flow = None
        st.session_state.drive_auth_url = None
        st.success("Google Drive との接続が完了しました。")


creds = ensure_drive_credentials()

if not creds:
    st.info("Google Drive へアップロードするには、Google アカウントの認証が必要です。")
    if st.button("Google アカウントと連携", key="start-drive-auth"):
        try:
            start_oauth_flow()
        except Exception as exc:  # noqa: BLE001
            st.error(f"認証フローを開始できませんでした: {exc}")

    if st.session_state.get("drive_auth_url"):
        st.markdown(
            f"1. [こちらのリンク]({st.session_state.drive_auth_url}) を開いてアクセスを許可してください。\n"
            "2. 表示された認証コードを以下に貼り付けて送信してください。"
        )
        with st.form("drive-auth-form"):
            auth_code = st.text_input("認証コード", key="drive-auth-code")
            submitted = st.form_submit_button("認証コードを送信")
        if submitted and auth_code:
            complete_oauth_flow(auth_code.strip())
elif creds:
    st.success("Google Drive に接続されています。")
    if st.button("Google アカウントの連携を解除", key="reset-drive-auth"):
        st.session_state.drive_credentials = None
        st.session_state.drive_auth_flow = None
        st.session_state.drive_auth_url = None
        st.info("Google アカウントの連携を解除しました。必要であれば再度認証してください。")


uploaded_file = st.file_uploader("Google Drive にアップロードするファイルを選択してください", key="drive-uploader")

if uploaded_file and ensure_drive_credentials():
    drive_folder_id = st.secrets.get("google_drive", {}).get("folder_id")

    if st.button("アップロードを実行", type="primary"):
        try:
            result = upload_file_to_drive(uploaded_file, folder_id=drive_folder_id)
        except Exception as exc:  # noqa: BLE001 - Streamlit surface for user feedback
            st.error(f"アップロードに失敗しました: {exc}")
        else:
            link = result.get("webViewLink")
            if link:
                st.success(f"アップロード完了: [{result['name']}]({link})")
            else:
                st.success(f"アップロード完了。ファイルID: {result['id']}")
