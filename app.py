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


def stream_dify(prompt: str):
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
        "response_mode": "streaming",
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
        stream=True,
    )
    response.raise_for_status()

    def _contains_noise(text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        prompt_lower = prompt_stripped.lower() if prompt_stripped else ""
        if prompt_lower and lowered == prompt_lower:
            return True
        if lowered in {"true", "false", "not empty"}:
            return True
        for substring in ("if/else", "variable assigner", "workflow_"):
            if substring in lowered:
                return True
        if lowered.endswith("succeeded"):
            return True
        compact_hex = lowered.replace("-", "")
        if re.fullmatch(r"[0-9a-f]{16,}", compact_hex):
            return True
        for prefix in (
            "answer ",
            "ragから情報抽出",
            "イベント名",
            "llm_回答生成_rag有り",
            ";unnamed:",
        ):
            if lowered.startswith(prefix):
                return True
        if re.fullmatch(r"回答\s*\(\d+\)", text.strip()):
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
        marker = ";Unnamed:"
        if marker in cleaned:
            _, _, remainder = cleaned.partition(marker)
            cleaned = f"{marker}{remainder}"
        cleaned = re.sub(r"(IF/ELSE)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(Answer\s+\d+(?:\s*\([^)]+\))?)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(Variable Assigner)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(workflow_[^\s]*)", r"\n\1", cleaned, flags=re.IGNORECASE)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
        cleaned = _strip_prompt_prefix(cleaned).strip()
        if not cleaned or _contains_noise(cleaned):
            return ""
        cleaned = _dedupe_repeated_text(cleaned)
        cleaned = _strip_prompt_prefix(cleaned).strip()
        return cleaned if _is_meaningful_text(cleaned) else ""

    def _iter_strings(value: object):
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, list):
            for item in value:
                yield from _iter_strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from _iter_strings(item)

    def _first_text(value: object) -> str:
        if value is None:
            return ""
        for candidate in _iter_strings(value):
            stripped = candidate.strip()
            if stripped:
                return candidate
        return ""

    raw_buffer = ""
    cleaned_answer = ""
    last_error_message = ""

    def _emit_cleaned() -> str:
        nonlocal cleaned_answer
        cleaned_full = _sanitize_text(raw_buffer)
        if not cleaned_full:
            return ""
        if cleaned_full.startswith(cleaned_answer):
            delta = cleaned_full[len(cleaned_answer) :]
        else:
            delta = cleaned_full
        cleaned_answer = cleaned_full
        return delta

    def _append_raw(raw_text: str) -> str:
        nonlocal raw_buffer
        if not raw_text:
            return ""
        raw_buffer += raw_text
        return _emit_cleaned()

    def _replace_raw(raw_text: str) -> str:
        nonlocal raw_buffer
        if not raw_text:
            return ""
        raw_buffer = raw_text
        return _emit_cleaned()

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
            raw_piece = _strip_prompt_prefix(chunk["answer_delta"])
            if not raw_piece:
                continue
            delta = _append_raw(raw_piece)
        elif isinstance(chunk.get("answer"), str):
            raw_piece = chunk["answer"]
            if not raw_piece.strip():
                print("[DIFY DEBUG] skip empty answer chunk", flush=True)
                continue
            delta = _replace_raw(raw_piece)
        elif isinstance(chunk.get("message"), dict):
            message = chunk["message"]
            raw_piece = _first_text(message.get("answer"))
            if not raw_piece:
                print("[DIFY DEBUG] skip empty message answer chunk", flush=True)
                continue
            delta = _replace_raw(raw_piece)
        else:
            event = chunk.get("event")
            data_section = chunk.get("data") if isinstance(chunk.get("data"), dict) else None
            if data_section and not cleaned_answer:
                raw_piece = _first_text(data_section.get("outputs")) or _first_text(data_section)
                if raw_piece:
                    delta = _replace_raw(raw_piece)
            if event == "error":
                last_error_message = (
                    _first_text(chunk.get("message"))
                    or _first_text(chunk.get("error"))
                    or _first_text(chunk.get("data"))
                    or "Dify からエラー応答を受信しました"
                )
                print("[DIFY DEBUG] error_event message=%s" % last_error_message, flush=True)
                break

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
            f"accumulated_len={len(cleaned_answer)}",
            flush=True,
        )

        if delta:
            yield delta

    if not cleaned_answer:
        if last_error_message:
            raise ValueError(_strip_history_prefixes(last_error_message.strip()))
        raise ValueError("Dify から応答が取得できませんでした")

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
