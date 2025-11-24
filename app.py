import io
import json
import os
import re
import subprocess
import time
from typing import Optional

import streamlit as st
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage
from google.oauth2 import service_account
import requests


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


def upload_file_to_gcs(uploaded_file, *, destination_name: Optional[str] = None) -> dict:
    """Upload an in-memory Streamlit file to Google Cloud Storage and return its metadata."""
    gcs_conf = _get_gcs_config()
    client = _build_gcs_client(gcs_conf)
    bucket = client.bucket(gcs_conf["bucket_name"])

    blob_name = destination_name or uploaded_file.name
    if gcs_conf["upload_prefix"]:
        blob_name = f"{gcs_conf['upload_prefix']}/{blob_name}"

    file_bytes = uploaded_file.getvalue()
    buffer = io.BytesIO(file_bytes)
    buffer.seek(0)

    blob = bucket.blob(blob_name)
    upload_kwargs = {
        "size": len(file_bytes),
        "content_type": uploaded_file.type or "application/octet-stream",
        "rewind": True,
    }
    if gcs_conf["predefined_acl"]:
        upload_kwargs["predefined_acl"] = gcs_conf["predefined_acl"]

    blob.upload_from_file(buffer, **upload_kwargs)

    result = {
        "bucket": gcs_conf["bucket_name"],
        "blob_name": blob_name,
        "gs_uri": f"gs://{gcs_conf['bucket_name']}/{blob_name}",
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

    history_prefixes: list[str] = []
    if history_lines:
        inputs["history"] = "\n".join(history_lines)
        print("[DIFY DEBUG] conversation history:")
        for line in history_lines:
            print(f"  {line}")
            stripped_line = line.strip()
            if not stripped_line:
                continue
            history_prefixes.append(stripped_line)
            if ":" in stripped_line:
                _, _, content_only = stripped_line.partition(":")
                content_only = content_only.strip()
                if content_only:
                    history_prefixes.append(content_only)

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
    st.set_page_config(page_title="ピボットAI壁打ち君", page_icon="💬", layout="wide")
    st.title("ピボットAI壁打ち君")

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
                print("[DEBUG] uploading file to GCS:", uploaded_sidebar_file.name)
                try:
                    result = upload_file_to_gcs(uploaded_sidebar_file)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"アップロードに失敗しました: {exc}")
                else:
                    st.session_state.dify_file_id = uploaded_sidebar_file.name
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
