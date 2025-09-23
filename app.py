import io
import json
from typing import Optional

import streamlit as st
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import requests


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# google drive周りの設定


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
        st.session_state.drive_credentials = creds

    creds = st.session_state.get("drive_credentials")
    if creds and getattr(creds, "valid", False):
        st.session_state.is_google_authenticated = True
    elif not creds:
        st.session_state.is_google_authenticated = False

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
        st.session_state.is_google_authenticated = False
    else:
        st.session_state.drive_credentials = flow.credentials
        st.session_state.drive_auth_flow = None
        st.session_state.drive_auth_url = None
        st.session_state.is_google_authenticated = True
        st.session_state.show_drive_uploader = False
        st.rerun()

# Dify周りの設定


def _get_dify_config():
    if "dify" not in st.secrets:
        raise ValueError("Dify API の設定が secrets.toml にありません")
    conf = st.secrets["dify"]
    api_key = conf.get("api_key")
    if not api_key:
        raise ValueError("Dify API キーが設定されていません")
    base_url = conf.get("base_url", "https://api.dify.ai").rstrip("/")
    user_identifier = conf.get("user", "streamlit-user")
    return api_key, base_url, user_identifier


def stream_dify(prompt: str):
    api_key, base_url, user_identifier = _get_dify_config()
    conversation_id = st.session_state.get("dify_conversation_id")

    inputs: dict[str, object] = {}

    file_id = st.session_state.get("dify_file_id", "").strip()
    if file_id:
        inputs["file_id"] = file_id

    is_rag_value = st.session_state.get("dify_is_rag", "")
    if isinstance(is_rag_value, str) and is_rag_value.strip().lower() == "true":
        inputs["is_rag"] = "true"

    system_prompt = st.session_state.get("dify_system_prompt", "").strip()
    if system_prompt:
        inputs["system_prompt"] = system_prompt

    payload = {
        "inputs": inputs,
        "query": prompt,
        "response_mode": "streaming",
        "user": user_identifier,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with requests.post(
        f"{base_url}/v1/chat-messages",
        json=payload,
        headers=headers,
        timeout=30,
        stream=True,
    ) as response:
        response.raise_for_status()

        accumulated_answer = ""

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if not raw_line.startswith("data:"):
                continue
            data_str = raw_line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue

            if conversation_id := chunk.get("conversation_id"):
                st.session_state.dify_conversation_id = conversation_id

            delta = ""
            if isinstance(chunk.get("answer_delta"), str):
                delta = chunk["answer_delta"]
                if delta:
                    accumulated_answer += delta
            elif isinstance(chunk.get("answer"), str):
                answer_full = chunk["answer"]
                if not answer_full:
                    print(
                        "[DIFY DEBUG] skip empty answer chunk",
                        flush=True,
                    )
                    continue
                if answer_full.startswith(accumulated_answer):
                    delta = answer_full[len(accumulated_answer) :]
                else:
                    delta = answer_full
                accumulated_answer = answer_full
            elif isinstance(chunk.get("message"), dict):
                message = chunk["message"]
                answer_full = message.get("answer") if isinstance(message.get("answer"), str) else ""
                if not answer_full:
                    print(
                        "[DIFY DEBUG] skip empty message answer chunk",
                        flush=True,
                    )
                    continue
                if answer_full:
                    if answer_full.startswith(accumulated_answer):
                        delta = answer_full[len(accumulated_answer) :]
                    else:
                        delta = answer_full
                    accumulated_answer = answer_full

            answer_str = chunk["answer"] if isinstance(chunk.get("answer"), str) else None
            message_obj = chunk.get("message") if isinstance(chunk.get("message"), dict) else None
            message_answer_str = (
                message_obj.get("answer") if isinstance(message_obj.get("answer"), str) else None
            ) if message_obj else None
            print(
                "[DIFY DEBUG] event="
                f"{chunk.get('event')} delta_len={len(delta) if delta else 0} "
                f"answer_len={len(answer_str) if answer_str is not None else 'None'} "
                f"message_answer_len={len(message_answer_str) if message_answer_str is not None else 'None'} "
                f"accumulated_len={len(accumulated_answer)}",
                flush=True,
            )

            if delta:
                yield delta

        if not accumulated_answer:
            raise ValueError("Dify から応答が取得できませんでした")

def main_ui():
    st.set_page_config(page_title="Chat Demo", page_icon="💬", layout="wide")
    st.title("Chat Demo")

    if "drive_credentials" not in st.session_state:
        st.session_state.drive_credentials = None
    if "drive_auth_flow" not in st.session_state:
        st.session_state.drive_auth_flow = None
    if "drive_auth_url" not in st.session_state:
        st.session_state.drive_auth_url = None
    if "is_google_authenticated" not in st.session_state:
        st.session_state.is_google_authenticated = False
    if "show_drive_uploader" not in st.session_state:
        st.session_state.show_drive_uploader = False

    creds = ensure_drive_credentials()

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "こんにちは！ご質問はありますか？"}
        ]
    if "dify_conversation_id" not in st.session_state:
        st.session_state.dify_conversation_id = None
    if "dify_file_id" not in st.session_state:
        st.session_state.dify_file_id = ""
    if "dify_is_rag" not in st.session_state:
        st.session_state.dify_is_rag = ""
    if "dify_system_prompt" not in st.session_state:
        st.session_state.dify_system_prompt = ""

    with st.sidebar:
        st.subheader("Dify オプション")
        creds_for_sidebar = st.session_state.get("drive_credentials")
        if creds_for_sidebar:
            uploaded_dify_file = st.file_uploader(
                "Dify 用ファイルを Google Drive にアップロード",
                key="sidebar-dify-uploader",
                help="アップロード後のファイルIDが自動で設定されます。",
            )
            if uploaded_dify_file and st.button("ファイルをアップロード", key="sidebar-dify-upload-btn"):
                try:
                    drive_folder_id = st.secrets.get("google_drive", {}).get("folder_id")
                    result = upload_file_to_drive(uploaded_dify_file, folder_id=drive_folder_id)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"アップロードに失敗しました: {exc}")
                else:
                    file_id = result.get("id")
                    if file_id:
                        st.session_state.dify_file_id = file_id
                        st.success("Google Drive にアップロードしました。file_id を更新しました。")
        else:
            st.info("Google Drive 認証後にファイルをアップロードできます。")

        st.text_input(
            "現在の file_id",
            value=st.session_state.get("dify_file_id", ""),
            help="Dify へ渡すファイルIDです。",
            disabled=True,
        )
        st.selectbox(
            "is_rag (任意)",
            options=["", "true"],
            key="dify_is_rag",
            help="RAG を利用したい場合は 'true' を選択します。",
        )
        st.text_area(
            "system_prompt (任意)",
            key="dify_system_prompt",
            help="モデルに渡すシステムプロンプトを上書きしたい場合に入力します。",
        )

        if st.button("会話をリセット", key="reset-conversastion"):
            st.session_state.messages = [
                {"role": "assistant", "content": "こんにちは！ご質問はありますか？"}
            ]
            for key in (
                "dify_conversation_id",
                "dify_file_id",
                "dify_is_rag",
                "dify_system_prompt",
            ):
                st.session_state.pop(key, None)
            st.rerun()

        st.markdown("---")
        st.subheader("Google Drive 接続")
        if not creds:
            st.info("Google Drive と連携するには下のボタンから認証してください。")
            if not st.session_state.is_google_authenticated and st.button("Google アカウントと連携", key="start-drive-auth"):
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
        else:
            st.success("Google Drive に接続されています。")
            if st.button("Google アカウントの連携を解除", key="reset-drive-auth"):
                st.session_state.drive_credentials = None
                st.session_state.drive_auth_flow = None
                st.session_state.drive_auth_url = None
                st.session_state.is_google_authenticated = False
                st.session_state.show_drive_uploader = False
                st.info("Google アカウントの連携を解除しました。必要であれば再度認証してください。")

    creds = st.session_state.get("drive_credentials")

    if creds:
        if not st.session_state.show_drive_uploader:
            if st.button("アップロードフォームを表示", key="toggle-drive-upload-show"):
                st.session_state.show_drive_uploader = True
                st.rerun()
        else:
            if st.button("アップロードフォームを閉じる", key="toggle-drive-upload-hide"):
                st.session_state.show_drive_uploader = False
                st.rerun()
            uploaded_file = st.file_uploader(
                "Google Drive にアップロードするファイルを選択してください",
                key="drive-uploader",
            )

            if uploaded_file and ensure_drive_credentials():
                drive_folder_id = st.secrets.get("google_drive", {}).get("folder_id")

                if st.button("アップロードを実行", type="primary"):
                    try:
                        result = upload_file_to_drive(uploaded_file, folder_id=drive_folder_id)
                    except Exception as exc:  # noqa: BLE001 - Streamlit surface for user feedback
                        st.error(f"アップロードに失敗しました: {exc}")
                    else:
                        uploaded_file_id = result.get("id")
                        if uploaded_file_id:
                            st.session_state.dify_file_id = uploaded_file_id
                        link = result.get("webViewLink")
                        if link:
                            st.success(f"アップロード完了: [{result['name']}]({link})")
                        else:
                            st.success(f"アップロード完了。ファイルID: {result['id']}")
    else:
        st.empty()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("メッセージを入力してください"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            response_container = st.empty()
            accumulated_response = ""
            try:
                with st.spinner("Dify から応答を取得しています..."):
                    for delta in stream_dify(prompt):
                        accumulated_response += delta
                        response_container.markdown(accumulated_response)
            except Exception as exc:  # noqa: BLE001 - surface API errors to user
                error_message = f"応答の取得に失敗しました: {exc}"
                st.session_state.messages.append({"role": "assistant", "content": error_message})
                response_container.error(error_message)
            else:
                st.session_state.messages.append({"role": "assistant", "content": accumulated_response})


main_ui()
