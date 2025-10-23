import io
import json
import os
import re
import subprocess
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


def fetch_dify_response(prompt: str) -> str:
    api_key, base_url, user_identifier = _get_dify_config()
    conversation_id = st.session_state.get("dify_conversation_id")

    inputs: dict[str, object] = {}

    prompt_stripped = prompt.strip()

    file_id = st.session_state.get("dify_file_id", "").strip()
    if file_id:
        inputs["file_id"] = file_id

    is_rag_value = st.session_state.get("dify_is_rag", "")
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
        "response_mode": "blocking",
        "user": user_identifier,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{base_url}/v1/chat-messages",
        json=payload,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    try:
        payload_data = response.json()
    except ValueError as exc:  # noqa: B904
        raise ValueError("Dify からJSON以外の応答を受信しました") from exc

    if conversation_id := payload_data.get("conversation_id"):
        st.session_state.dify_conversation_id = conversation_id

    def _contains_noise(text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if lowered in {"true", "false", "not empty"}:
            return True
        if prompt_stripped and lowered == prompt_stripped.lower():
            return True
        if "if/else" in lowered:
            return True
        if "variable assigner" in lowered:
            return True
        if "workflow_" in lowered:
            return True
        if lowered.endswith("succeeded"):
            return True
        if re.fullmatch(r"[0-9a-f]{16,}", lowered.replace("-", "")):
            return True
        if lowered.startswith("answer "):
            return True
        if lowered.startswith("ragから情報抽出"):
            return True
        if lowered.startswith("イベント名"):
            return True
        if lowered.startswith("llm_回答生成_rag有り"):
            return True
        if re.fullmatch(r"回答\s*\(\d+\)", lowered):
            return True
        if lowered.startswith(";unnamed:"):
            return True
        return False

    def _strip_history_prefixes(text: str) -> str:
        if not text:
            return ""
        cleaned = text.lstrip()
        history_candidates = [prefix.strip() for prefix in history_prefixes if prefix.strip()]
        noise_prefix_patterns = (
            r"(?i)^assistant[:：]\s*",
            r"(?i)^user[:：]\s*",
            r"(?i)^system[:：]\s*",
            r"(?i)^ragから情報抽出[^\s：#-]*[:：#\s-]*",
            r"(?i)^イベント名[^\s]*\s*",
            r"(?i)^llm_回答生成_rag有り[:：#\s-]*",
            r"(?i)^回答\s*\(\d+\)\s*",
        )
        changed = True
        while cleaned and changed:
            changed = False
            for prefix in history_candidates:
                if prefix and cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix) :].lstrip("：:、,。 \u3000")
                    changed = True
            if cleaned:
                for pattern in noise_prefix_patterns:
                    new_cleaned = re.sub(pattern, "", cleaned, count=1)
                    if new_cleaned != cleaned:
                        cleaned = new_cleaned.lstrip("：:、,。 \u3000")
                        changed = True
        return cleaned

    def _strip_prompt_prefix(text: str) -> str:
        if not text:
            return ""
        trimmed = text.lstrip()
        trimmed = _strip_history_prefixes(trimmed)
        if prompt_stripped and trimmed.startswith(prompt_stripped):
            trimmed = trimmed[len(prompt_stripped) :].lstrip("：:、,。 \u3000")
        return trimmed

    def _is_meaningful_text(text: str) -> bool:
        if not text:
            return False
        stripped = text.strip()
        if not stripped:
            return False
        stripped = _strip_prompt_prefix(stripped)
        if not stripped:
            return False
        if stripped == user_identifier:
            return False
        if prompt_stripped and stripped == prompt_stripped:
            return False
        if _contains_noise(stripped):
            return False
        if not (
            re.search(r"\s", stripped)
            or re.search(r"[。．！？!?]", stripped)
            or re.search(r"[\u3000-\u303F\u3040-\u30ff\u4e00-\u9faf]", stripped)
        ):
            return False
        return True

    def _dedupe_repeated_text(text: str) -> str:
        if not text:
            return ""
        stripped = text.strip()
        if not stripped:
            return ""
        half = len(stripped) // 2
        if len(stripped) % 2 == 0 and stripped[:half] == stripped[half:]:
            return _dedupe_repeated_text(stripped[:half])
        paragraphs = re.split(r"\n{2,}", stripped)
        filtered_paragraphs: list[str] = []
        seen_paragraphs: set[str] = set()
        for paragraph in paragraphs:
            para_clean = paragraph.strip()
            if not para_clean:
                continue
            if para_clean in seen_paragraphs:
                continue
            seen_paragraphs.add(para_clean)
            filtered_paragraphs.append(paragraph.strip())
        if filtered_paragraphs:
            return "\n\n".join(filtered_paragraphs)
        return stripped

    def _sanitize_text(text: str) -> str:
        if not text:
            return ""
        cleaned = _strip_prompt_prefix(text)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"(IF/ELSE)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(Answer\s+\d+(?:\s*\([^)]+\))?)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(Variable Assigner)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(workflow_[^\s]*)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
        cleaned = _strip_prompt_prefix(cleaned).strip()
        if not cleaned or _contains_noise(cleaned.lower()):
            return ""
        cleaned = _dedupe_repeated_text(cleaned)
        cleaned = _strip_prompt_prefix(cleaned).strip()
        return cleaned if _is_meaningful_text(cleaned) else ""

    def _extract_text_payload(payload: object) -> str:
        candidates: list[str] = []

        def _collect(obj: object) -> None:
            if isinstance(obj, str):
                text = obj.strip()
                if text:
                    candidates.append(text)
                return
            if isinstance(obj, list):
                for item in obj:
                    _collect(item)
                return
            if isinstance(obj, dict):
                value_fields = (
                    obj.get("value"),
                    obj.get("answer"),
                    obj.get("result"),
                    obj.get("output_text"),
                    obj.get("text"),
                    obj.get("content"),
                    obj.get("message"),
                    obj.get("data"),
                )
                for field in value_fields:
                    if field is not None:
                        _collect(field)
                for key, value in obj.items():
                    if key in {"value", "answer", "result", "output_text", "text", "content", "message", "data"}:
                        continue
                    _collect(value)

        _collect(payload)
        unique_candidates: list[str] = []
        seen_candidates: set[str] = set()
        for item in candidates:
            if item in seen_candidates:
                continue
            seen_candidates.add(item)
            unique_candidates.append(item)
        candidates = unique_candidates

        meaningful: list[str] = []
        for text in candidates:
            if _is_meaningful_text(text):
                meaningful.append(text)

        for text in reversed(meaningful):
            cleaned = _sanitize_text(text)
            if cleaned:
                return cleaned

        for text in reversed(candidates):
            cleaned = _sanitize_text(text)
            if cleaned:
                return cleaned

        return ""

    primary_answer = payload_data.get("answer") if isinstance(payload_data.get("answer"), str) else ""
    cleaned_primary = _sanitize_text(primary_answer)
    if cleaned_primary:
        return cleaned_primary

    fallback_answer = _extract_text_payload(payload_data)
    if fallback_answer:
        return fallback_answer

    error_message = payload_data.get("message") or payload_data.get("error")
    if isinstance(error_message, str) and error_message.strip():
        raise ValueError(_strip_history_prefixes(error_message.strip()))

    raise ValueError("Dify から応答が取得できませんでした")

def main_ui():
    st.set_page_config(page_title="Pivot AI", page_icon="💬", layout="wide")
    st.title("Pivot AI")

    gcs_config_error: Optional[str] = None
    try:
        _get_gcs_config()
    except Exception as exc:  # noqa: BLE001
        gcs_config_error = str(exc)

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "こんにちは！ご質問はありますか？"}
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
        st.subheader("Dify オプション")
        st.selectbox(
            "is_rag (任意)",
            options=["true", ""],
            key="dify_is_rag",
            help="RAG を利用したい場合は 'true' を、利用しない場合は空を選択します。",
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
            try:
                with st.spinner("Dify から応答を取得しています..."):
                    answer_text = fetch_dify_response(prompt)
            except Exception as exc:  # noqa: BLE001 - surface API errors to user
                error_message = f"応答の取得に失敗しました: {exc}"
                st.session_state.messages.append({"role": "assistant", "content": error_message})
                st.error(error_message)
            else:
                st.session_state.messages.append({"role": "assistant", "content": answer_text})
                st.markdown(answer_text)


main_ui()
