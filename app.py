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

    if history_lines:
        inputs["history"] = "\n".join(history_lines)
        print("[DIFY DEBUG] conversation history:")
        for line in history_lines:
            print(f"  {line}")

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
        last_error_message = ""
        # prompt_stripped already computed earlier.

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
            return False

        def _strip_prompt_prefix(text: str) -> str:
            if not text:
                return ""
            trimmed = text.lstrip()
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

        def _extract_text_payload(payload: object) -> str:
            """Return a human-friendly string extracted from workflow outputs."""
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
                    # Prefer explicit value fields first
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
                    # Fall back to any remaining values
                    for key, value in obj.items():
                        if key in {"value", "answer", "result", "output_text", "text", "content", "message", "data"}:
                            continue
                        _collect(value)

            def _dedupe_repeated_text(text: str) -> str:
                if not text:
                    return ""
                stripped = text.strip()
                if not stripped:
                    return ""
                # Check if the text is exactly the same repeated twice consecutively.
                half = len(stripped) // 2
                if len(stripped) % 2 == 0 and stripped[:half] == stripped[half:]:
                    return _dedupe_repeated_text(stripped[:half])
                # Remove duplicated sentences.
                sentences = re.split(r"(?<=[。．！？!?])\s*", stripped)
                filtered: list[str] = []
                seen_sentences: set[str] = set()
                for sentence in sentences:
                    cleaned_sentence = sentence.strip()
                    if not cleaned_sentence:
                        continue
                    if cleaned_sentence in seen_sentences:
                        continue
                    seen_sentences.add(cleaned_sentence)
                    filtered.append(cleaned_sentence)
                if filtered:
                    return _strip_prompt_prefix("".join(filtered))
                return _strip_prompt_prefix(stripped)

            def _sanitize_text(text: str) -> str:
                if not text:
                    return ""
                cleaned = text.strip()
                cleaned = _strip_prompt_prefix(cleaned)
                normalized = re.sub(r"(IF/ELSE)", r"\n\1", cleaned, flags=re.IGNORECASE)
                normalized = re.sub(r"(Answer\s+\d+(?:\s*\([^)]+\))?)", r"\n\1", normalized, flags=re.IGNORECASE)
                normalized = re.sub(r"(Variable Assigner)", r"\n\1", normalized, flags=re.IGNORECASE)
                normalized = re.sub(r"(workflow_[^\s]*)", r"\n\1", normalized, flags=re.IGNORECASE)
                lines = [line.strip() for line in normalized.splitlines() if line.strip()]

                def _strip_parenthetical(line: str) -> str:
                    if "(" in line and ")" in line:
                        prefix, suffix = line.split("(", 1)
                        suffix_lower = suffix.lower()
                        if prefix.strip() and any(
                            marker in suffix_lower for marker in ("you can", "for example", "etc.", "例えば", "ｅｔｃ")
                        ):
                            return prefix.strip()
                    return line

                for line in lines:
                    candidate = _strip_parenthetical(line)
                    candidate = _strip_prompt_prefix(candidate)
                    if candidate.lower().startswith("answer "):
                        continue
                    if _is_meaningful_text(candidate):
                        return _dedupe_repeated_text(candidate)

                for line in reversed(lines):
                    candidate = _strip_parenthetical(line)
                    candidate = _strip_prompt_prefix(candidate)
                    if candidate.lower().startswith("answer "):
                        continue
                    if _is_meaningful_text(candidate):
                        return _dedupe_repeated_text(candidate)

                candidate = _strip_parenthetical(cleaned)
                candidate = _strip_prompt_prefix(candidate)
                return _dedupe_repeated_text(candidate) if _is_meaningful_text(candidate) else ""

            _collect(payload)
            # Deduplicate candidates while preserving order.
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
                delta_candidate = chunk["answer_delta"]
                if not delta_candidate:
                    continue
                delta_candidate = _strip_prompt_prefix(delta_candidate)
                if not delta_candidate or _contains_noise(delta_candidate):
                    continue
                delta_candidate = _dedupe_repeated_text(delta_candidate)
                if not delta_candidate:
                    continue
                delta = delta_candidate
                accumulated_answer += delta
            elif isinstance(chunk.get("answer"), str):
                answer_full_raw = chunk["answer"]
                if not answer_full_raw:
                    print(
                        "[DIFY DEBUG] skip empty answer chunk",
                        flush=True,
                    )
                    continue
                answer_full = _extract_text_payload(answer_full_raw)
                if not answer_full:
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
                answer_full = _extract_text_payload(answer_full)
                if not answer_full:
                    continue
                if answer_full.startswith(accumulated_answer):
                    delta = answer_full[len(accumulated_answer) :]
                else:
                    delta = answer_full
                accumulated_answer = answer_full
            else:
                event = chunk.get("event")
                data_section = chunk.get("data") if isinstance(chunk.get("data"), dict) else None
                if data_section:
                    outputs_text = _extract_text_payload(data_section.get("outputs"))
                    if not outputs_text:
                        outputs_text = _extract_text_payload(data_section)
                    if outputs_text:
                        if outputs_text.startswith(accumulated_answer):
                            delta = outputs_text[len(accumulated_answer) :]
                        else:
                            delta = outputs_text
                        accumulated_answer = outputs_text
                if event == "error":
                    last_error_message = (
                        chunk.get("message")
                        or chunk.get("error")
                        or _extract_text_payload(chunk.get("data"))
                        or "Dify からエラー応答を受信しました"
                    )
                    print(
                        "[DIFY DEBUG] error_event "
                        f"message={last_error_message}",
                        flush=True,
                    )
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
                f"accumulated_len={len(accumulated_answer)}",
                flush=True,
            )

            if delta:
                yield delta

        if not accumulated_answer:
            if last_error_message:
                raise ValueError(last_error_message)
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
