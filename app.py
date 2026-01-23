import io
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote as url_quote

import streamlit as st
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage
from google.oauth2 import service_account
import requests
import gspread


def _get_gcs_config() -> dict:
    if "gcs" not in st.secrets:
        raise ValueError("Google Cloud Storage の設定が secrets.toml にありません")

    gcs_conf = st.secrets["gcs"]
    bucket_name = gcs_conf.get("bucket_name")
    if not bucket_name:
        raise ValueError("GCS バケット名が設定されていません")

    service_account_info = gcs_conf.get("service_account")
    if not service_account_info:
        raise ValueError("GCS の service_account 情報が設定されていません")

    if isinstance(service_account_info, str):
        try:
            service_account_info = json.loads(service_account_info)
        except json.JSONDecodeError as exc:
            raise ValueError("service_account は JSON 文字列または辞書で指定してください") from exc

    project_id = gcs_conf.get("project_id")
    upload_prefix = gcs_conf.get("upload_prefix")
    if isinstance(upload_prefix, str):
        upload_prefix = upload_prefix.strip().strip("/")
        if not upload_prefix:
            upload_prefix = None
    else:
        upload_prefix = None

    make_public = gcs_conf.get("make_public", False)
    if isinstance(make_public, str):
        make_public = make_public.strip().lower() in {"1", "true", "yes", "on"}
    else:
        make_public = bool(make_public)

    predefined_acl = gcs_conf.get("predefined_acl")
    if isinstance(predefined_acl, str) and not predefined_acl.strip():
        predefined_acl = None

    return {
        "bucket_name": bucket_name,
        "project_id": project_id,
        "service_account": service_account_info,
        "upload_prefix": upload_prefix,
        "make_public": make_public,
        "predefined_acl": predefined_acl,
    }


def _build_gcs_client(gcs_conf: dict) -> storage.Client:
    credentials = service_account.Credentials.from_service_account_info(gcs_conf["service_account"])
    project_id = gcs_conf["project_id"] or getattr(credentials, "project_id", None)
    if not project_id:
        raise ValueError("GCS の project_id が特定できません")
    return storage.Client(project=project_id, credentials=credentials)


def convert_pptx_to_pdf(pptx_bytes: bytes, original_filename: str) -> tuple[bytes, str]:
    """Convert PPTX file to PDF using LibreOffice.

    Args:
        pptx_bytes: The PPTX file content as bytes
        original_filename: Original filename for reference

    Returns:
        tuple of (pdf_bytes, pdf_filename)

    Raises:
        RuntimeError: If conversion fails
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        # Save PPTX to temporary file
        pptx_path = os.path.join(temp_dir, original_filename)
        with open(pptx_path, "wb") as f:
            f.write(pptx_bytes)

        # Convert to PDF using LibreOffice
        try:
            result = subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", temp_dir,
                    pptx_path
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"LibreOffice conversion failed: {exc.stderr}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("LibreOffice conversion timed out") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "LibreOffice (soffice) not found. Please install LibreOffice on the server."
            ) from exc

        # Find the generated PDF file
        pdf_filename = os.path.splitext(original_filename)[0] + ".pdf"
        pdf_path = os.path.join(temp_dir, pdf_filename)

        if not os.path.exists(pdf_path):
            raise RuntimeError(f"PDF file was not generated: {pdf_path}")

        # Read the PDF content
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        return pdf_bytes, pdf_filename


def upload_file_to_gcs(uploaded_file, *, destination_name: Optional[str] = None) -> dict:
    """Upload an in-memory Streamlit file to Google Cloud Storage and return its metadata.

    If the file is a PPTX, it will be converted to PDF before uploading.
    """
    gcs_conf = _get_gcs_config()
    client = _build_gcs_client(gcs_conf)
    bucket = client.bucket(gcs_conf["bucket_name"])

    # Check if file is PPTX and convert to PDF
    original_filename = uploaded_file.name
    file_bytes = uploaded_file.getvalue()
    content_type = uploaded_file.type or "application/octet-stream"

    is_pptx = (
        original_filename.lower().endswith(".pptx") or
        content_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )

    if is_pptx:
        try:
            file_bytes, converted_filename = convert_pptx_to_pdf(file_bytes, original_filename)
            blob_name = destination_name or converted_filename
            content_type = "application/pdf"
        except RuntimeError as exc:
            raise ValueError(f"PPTX to PDF conversion failed: {exc}") from exc
    else:
        blob_name = destination_name or original_filename

    if gcs_conf["upload_prefix"]:
        blob_name = f"{gcs_conf['upload_prefix']}/{blob_name}"

    buffer = io.BytesIO(file_bytes)
    buffer.seek(0)

    blob = bucket.blob(blob_name)
    upload_kwargs = {
        "size": len(file_bytes),
        "content_type": content_type,
        "rewind": True,
    }
    if gcs_conf["predefined_acl"]:
        upload_kwargs["predefined_acl"] = gcs_conf["predefined_acl"]

    blob.upload_from_file(buffer, **upload_kwargs)

    # Extract just the filename from blob_name (remove prefix if present)
    uploaded_filename = blob_name.split('/')[-1] if '/' in blob_name else blob_name

    result = {
        "bucket": gcs_conf["bucket_name"],
        "blob_name": blob_name,
        "gs_uri": f"gs://{gcs_conf['bucket_name']}/{blob_name}",
        "uploaded_filename": uploaded_filename,
    }
    if gcs_conf["make_public"]:
        try:
            blob.make_public()
        except gcs_exceptions.GoogleAPICallError as exc:
            result[
                "public_url_error"
            ] = (
                "Uniform bucket-level access が有効なバケットでは ACL による公開設定が行えません。"
                " secrets.toml の make_public を false に設定するか、バケット設定を見直してください。"
                f" (詳細: {exc})"
            )
        except Exception as exc:  # noqa: BLE001
            result[
                "public_url_error"
            ] = f"公開URLの作成に失敗しました: {exc}. make_public を false に設定することで回避できます。"
        else:
            result["public_url"] = blob.public_url

    return result


def _get_app_version(default: str = "dev") -> str:
    """Resolve app version from env or git metadata."""
    env_version = os.getenv("APP_VERSION", "").strip()
    if env_version:
        return env_version

    git_commands = (
        ["git", "describe", "--tags", "--always"],
        ["git", "rev-parse", "--short", "HEAD"],
    )
    for command in git_commands:
        try:
            version = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if version:
            return version

    return default


APP_VERSION = _get_app_version()

# Authentication with Google Sheets


def _parse_date(date_str: str) -> datetime | None:
    """Parse date string from spreadsheet (supports YYYY/MM/DD and YYYY-MM-DD formats)."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _check_date_permission(start_date_str: str, end_date_str: str) -> bool:
    """Check if current time (JST) is within the permission period.

    Permission is granted from perStartDate 0:00 to perEndDate 23:59 (JST).
    """
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST)

    start_date = _parse_date(start_date_str)
    end_date = _parse_date(end_date_str)

    if start_date is None or end_date is None:
        return False

    # Set start to 0:00 JST and end to 23:59:59 JST
    start_datetime = start_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=JST)
    end_datetime = end_date.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=JST)

    return start_datetime <= now_jst <= end_datetime


def check_user_credentials(user_id: str, password: str) -> tuple[bool, bool]:
    """Check user credentials against Google Sheets.

    Returns:
        tuple[bool, bool]: (is_authenticated, has_permission)
    """
    try:
        if "auth" not in st.secrets:
            raise ValueError("認証設定が secrets.toml にありません")

        auth_conf = st.secrets["auth"]
        spreadsheet_id = auth_conf.get("spreadsheet_id")
        if not spreadsheet_id:
            raise ValueError("スプレッドシートIDが設定されていません")

        # Use the same service account as GCS
        gcs_conf = _get_gcs_config()
        credentials = service_account.Credentials.from_service_account_info(
            gcs_conf["service_account"],
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )

        # Initialize gspread client
        gc = gspread.authorize(credentials)

        # Open the spreadsheet and get "アクセス管理" sheet
        spreadsheet = gc.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet("アクセス管理")

        # Get all records
        records = worksheet.get_all_records()

        # Check credentials
        for record in records:
            if str(record.get("id", "")).strip() == user_id.strip():
                if str(record.get("password", "")).strip() == password.strip():
                    # Check permission by date range (perStartDate to perEndDate)
                    start_date = str(record.get("perStartDate", ""))
                    end_date = str(record.get("perEndDate", ""))
                    has_permission = _check_date_permission(start_date, end_date)
                    return True, has_permission
                else:
                    return False, False

        return False, False

    except Exception as exc:
        st.error(f"認証エラー: {exc}")
        return False, False


def show_login_page():
    """Display login page and handle authentication."""
    st.set_page_config(page_title="PIVOT AI - ログイン", page_icon="🔐", layout="centered")

    st.title("🔐 PIVOT AI ログイン")
    st.markdown("---")

    with st.form("login_form"):
        user_id = st.text_input("ユーザーID", placeholder="IDを入力してください")
        password = st.text_input("パスワード", type="password", placeholder="パスワードを入力してください")
        submit_button = st.form_submit_button("ログイン", use_container_width=True)

        if submit_button:
            if not user_id or not password:
                st.error("ユーザーIDとパスワードを入力してください")
            else:
                with st.spinner("認証中..."):
                    is_authenticated, has_permission = check_user_credentials(user_id, password)

                    if is_authenticated:
                        if has_permission:
                            st.session_state.authenticated = True
                            st.session_state.user_id = user_id
                            st.success("ログインに成功しました!")
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error("アクセス権限がありません。管理者にお問い合わせください。")
                    else:
                        st.error("ユーザーIDまたはパスワードが間違っています")

    st.markdown("---")
    st.caption("")# © 神社仏閣オンライン株式会社とか入れるならここに


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

    prompt_stripped = prompt.strip()

    file_id = st.session_state.get("dify_file_id", "").strip()
    if file_id:
        inputs["file_id"] = file_id

    is_rag_value = st.session_state.get("dify_is_rag", "true")
    if isinstance(is_rag_value, str) and is_rag_value.strip().lower() == "true":
        inputs["is_rag"] = "true"

    system_prompt = st.session_state.get("dify_system_prompt", "").strip()
    if system_prompt:
        inputs["system_prompt"] = system_prompt

    history_lines: list[str] = []
    for message in st.session_state.get("messages", []):
        role = message.get("role", "").strip()
        content = message.get("content", "")
        if not role or not content:
            continue
        content_clean = " ".join(content.strip().splitlines())
        if not content_clean:
            continue
        history_lines.append(f"{role}:{content_clean}")

    if prompt_stripped and history_lines:
        last_entry = history_lines[-1]
        expected_last = f"user:{prompt_stripped}"
        if last_entry == expected_last:
            history_lines.pop()

    if history_lines:
        inputs["history"] = "\n".join(history_lines)

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
        "Accept": "text/event-stream",
    }

    def _blocking_request(retries: int = 2, delay: float = 1.5) -> str:
        blocking_payload = dict(payload)
        blocking_payload["response_mode"] = "blocking"
        blocking_headers = dict(headers)
        blocking_headers["Accept"] = "application/json"

        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    f"{base_url}/v1/chat-messages",
                    json=blocking_payload,
                    headers=blocking_headers,
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.exceptions.RequestException:
                if attempt == retries:
                    return ""
            else:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    if conversation_id := data.get("conversation_id"):
                        st.session_state.dify_conversation_id = conversation_id
                    answer_text = ""
                    if isinstance(data.get("answer"), str):
                        answer_text = data["answer"]
                    if answer_text:
                        return answer_text
            if delay > 0 and attempt < retries:
                time.sleep(delay)
        return ""

    use_blocking_initial = bool(file_id)

    if use_blocking_initial:
        blocking_answer = _blocking_request(retries=3, delay=1.5)
        if blocking_answer:
            yield blocking_answer
            return
        # Fall back to streaming if blocking failed; continue below.

    try:
        response = requests.post(
            f"{base_url}/v1/chat-messages",
            json=payload,
            headers=headers,
            timeout=60,
            stream=True,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        fallback_answer = _blocking_request()
        if fallback_answer:
            yield fallback_answer
            return
        raise ValueError("Dify への接続に失敗しました") from exc

    try:
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
            elif isinstance(chunk.get("answer"), str):
                delta = chunk["answer"]
            elif isinstance(chunk.get("message"), dict):
                message = chunk["message"]
                if isinstance(message.get("answer"), str):
                    delta = message["answer"]
            
            # Handle error events
            if chunk.get("event") == "error":
                error_msg = chunk.get("message") or chunk.get("error") or "Dify Error"
                print(f"[DIFY ERROR] {error_msg}", flush=True)

            if delta:
                yield delta

    except requests.exceptions.RequestException as exc:
        fallback_answer = _blocking_request()
        if fallback_answer:
            yield fallback_answer
            return
        raise ValueError("Dify ストリーミング中に接続が中断されました") from exc
    finally:
        response.close()

def main_ui():
    # Check authentication status
    if "authenticated" not in st.session_state or not st.session_state.authenticated:
        show_login_page()
        return

    st.set_page_config(page_title="PIVOT AI", page_icon="💬", layout="wide")
    st.title("PIVOT AI")

    gcs_config_error: Optional[str] = None
    try:
        _get_gcs_config()
    except Exception as exc:  # noqa: BLE001
        gcs_config_error = str(exc)

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "こんにちは！ピボットの知識を持ったAIです。起業やビジネスについて気軽に質問してください!"}
        ]
    if "dify_conversation_id" not in st.session_state:
        st.session_state.dify_conversation_id = None
    if "dify_file_id" not in st.session_state:
        st.session_state.dify_file_id = ""
    if "dify_is_rag" not in st.session_state:
        st.session_state.dify_is_rag = "true"
    if "dify_system_prompt" not in st.session_state:
        st.session_state.dify_system_prompt = ""

    with st.sidebar:
        # User info and logout
        st.markdown(f"**ログイン中:** {st.session_state.get('user_id', 'Unknown')}")
        if st.button("ログアウト", key="logout-button", use_container_width=True):
            st.session_state.authenticated = False
            st.session_state.user_id = None
            st.rerun()

        st.markdown("---")
        st.subheader("AIオプション")
        st.selectbox(
            "is_rag (任意)",
            options=["true", "false"],
            key="dify_is_rag",
            help="RAG を利用したい場合は 'true' を、利用しない場合は 'false' を選択します。",
        )
        st.text_area(
            "system_prompt (任意)",
            key="dify_system_prompt",
            help="モデルに渡すシステムプロンプトを上書きしたい場合に入力します。",
        )

        if gcs_config_error:
            st.error(f"GCS 設定エラー: {gcs_config_error}")
        else:
            st.subheader("ファイルアップロード")
            uploaded_sidebar_file = st.file_uploader(
                "アップロードするファイルを選択してください",
                key="sidebar-gcs-uploader",
                help="ファイルを選択すると自動でアップロードします。",
            )
            if uploaded_sidebar_file:
                print("[DEBUG] uploading file to GCS:", uploaded_sidebar_file.name, flush=True)
                try:
                    result = upload_file_to_gcs(uploaded_sidebar_file)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"アップロードに失敗しました: {exc}")
                else:
                    # Use the uploaded filename (which may be converted to PDF)
                    uploaded_filename = result.get("uploaded_filename", uploaded_sidebar_file.name)
                    encoded_filename = url_quote(uploaded_filename, safe='')
                    st.session_state.dify_file_id = encoded_filename
                    print(f"[DEBUG] Original filename: {uploaded_sidebar_file.name}", flush=True)
                    print(f"[DEBUG] Uploaded filename: {uploaded_filename}", flush=True)
                    print(f"[DEBUG] Encoded file_id for Dify: {encoded_filename}", flush=True)
                    st.success("アップロード完了")
                    if result.get("public_url"):
                        st.info("公開URL は GCS でご確認ください。")
                    if result.get("public_url_error"):
                        st.warning(result["public_url_error"])

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
        st.markdown(
            "[バグレポートはこちら](https://forms.gle/4EBnjTLd68kFvAma7)",
            help="Google Form で不具合を報告できます。",
        )
        st.text(f"バージョン: {APP_VERSION}")

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
                with st.spinner("AIから応答を取得しています..."):
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
