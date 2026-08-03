from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidSignature

from .models import FeishuIntegrationSpec, ProjectSpec, QQIntegrationSpec
from .workflow_runner import TERMINAL_RUN_STATES, WorkflowManager


class EventDeduplicator:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._seen = {item: expires for item, expires in self._seen.items() if expires > now}
            if key in self._seen:
                return False
            self._seen[key] = now + self.ttl_seconds
            return True


class PlatformIntegrationManager:
    def __init__(self, workflows: WorkflowManager) -> None:
        self.workflows = workflows
        self.deduplicator = EventDeduplicator()
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    @staticmethod
    def environment(env_file: str | None) -> dict[str, str]:
        values = os.environ.copy()
        if env_file:
            path = Path(env_file).expanduser()
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() and not values.get(key.strip()):
                        values[key.strip()] = value.strip().strip("'\"")
        return values

    def status(self, project: ProjectSpec) -> dict[str, list[dict[str, Any]]]:
        def ready(item: Any, names: list[str]) -> dict[str, Any]:
            env = self.environment(item.env_file)
            missing = [name for name in names if not env.get(name)]
            return {"id": item.id, "name": item.name, "workflow_id": item.workflow_id, "ready": not missing, "missing_env": missing, "auto_reply": item.auto_reply}
        return {
            "feishu": [ready(item, [item.app_id_env, item.app_secret_env, item.verification_token_env]) for item in project.integrations.feishu],
            "qq": [ready(item, [item.app_id_env, item.secret_env]) for item in project.integrations.qq],
        }

    @staticmethod
    def require_feishu(project: ProjectSpec, integration_id: str) -> FeishuIntegrationSpec:
        item = next((value for value in project.integrations.feishu if value.id == integration_id), None)
        if item is None:
            raise KeyError(integration_id)
        return item

    @staticmethod
    def require_qq(project: ProjectSpec, integration_id: str) -> QQIntegrationSpec:
        item = next((value for value in project.integrations.qq if value.id == integration_id), None)
        if item is None:
            raise KeyError(integration_id)
        return item

    def handle_feishu(self, project: ProjectSpec, config: FeishuIntegrationSpec, raw: bytes, headers: dict[str, str]) -> dict[str, Any]:
        env = self.environment(config.env_file)
        token = env.get(config.verification_token_env, "")
        if not token:
            raise RuntimeError(f"缺少环境变量 {config.verification_token_env}")
        self._verify_feishu_signature(raw, headers, env.get(config.encrypt_key_env or "", ""))
        payload = json.loads(raw or b"{}")
        if "encrypt" in payload:
            key = env.get(config.encrypt_key_env or "", "")
            if not key:
                raise RuntimeError(f"加密事件缺少环境变量 {config.encrypt_key_env}")
            payload = json.loads(self._decrypt_feishu(str(payload["encrypt"]), key))
        if payload.get("type") == "url_verification":
            if payload.get("token") != token:
                raise PermissionError("飞书 verification token 无效")
            return {"challenge": payload.get("challenge", "")}
        header = payload.get("header", {})
        if header.get("token") != token:
            raise PermissionError("飞书 verification token 无效")
        event_id = str(header.get("event_id") or hashlib.sha256(raw).hexdigest())
        if not self.deduplicator.accept(f"feishu:{config.id}:{event_id}"):
            return {"code": 0, "duplicate": True}
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        content = message.get("content", "")
        try:
            parsed_content = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            parsed_content = {"text": content}
        workflow_input = {"platform": "feishu", "integration_id": config.id, "event_id": event_id, "event_type": header.get("event_type"), "message_id": message.get("message_id"), "chat_id": message.get("chat_id"), "chat_type": message.get("chat_type"), "sender": sender, "message_type": message.get("message_type"), "content": parsed_content, "raw_event": event}
        run = self.workflows.start(project, config.workflow_id, {"input": workflow_input})
        if config.auto_reply and message.get("message_id"):
            threading.Thread(target=self._reply_feishu_after_run, args=(config, env, run.id, str(message["message_id"])), daemon=True).start()
        return {"code": 0, "run_id": run.id}

    @staticmethod
    def _verify_feishu_signature(raw: bytes, headers: dict[str, str], encrypt_key: str) -> None:
        signature = headers.get("x-lark-signature", "")
        if not signature:
            return
        timestamp, nonce = headers.get("x-lark-request-timestamp", ""), headers.get("x-lark-request-nonce", "")
        _require_fresh_timestamp(timestamp)
        expected = hashlib.sha256(timestamp.encode() + nonce.encode() + encrypt_key.encode() + raw).hexdigest()
        if not _constant_time_equal(signature, expected):
            raise PermissionError("飞书回调签名无效")

    @staticmethod
    def _decrypt_feishu(encrypted: str, encrypt_key: str) -> str:
        ciphertext = base64.b64decode(encrypted)
        key = hashlib.sha256(encrypt_key.encode()).digest()
        decryptor = Cipher(algorithms.AES(key), modes.CBC(ciphertext[:16])).decryptor()
        padded = decryptor.update(ciphertext[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode()

    def handle_qq(self, project: ProjectSpec, config: QQIntegrationSpec, raw: bytes, headers: dict[str, str]) -> dict[str, Any]:
        env = self.environment(config.env_file)
        secret = env.get(config.secret_env, "")
        if not secret:
            raise RuntimeError(f"缺少环境变量 {config.secret_env}")
        payload = json.loads(raw or b"{}")
        if int(payload.get("op", 0)) == 13:
            plain_token, event_ts = str(payload.get("d", {}).get("plain_token", "")), str(payload.get("d", {}).get("event_ts", ""))
            return {"plain_token": plain_token, "signature": self._qq_private_key(secret).sign((event_ts + plain_token).encode()).hex()}
        self._verify_qq_signature(raw, headers, secret)
        event_id = str(payload.get("id") or hashlib.sha256(raw).hexdigest())
        if not self.deduplicator.accept(f"qq:{config.id}:{event_id}"):
            return {"op": 12, "duplicate": True}
        event_type, data = str(payload.get("t", "")), payload.get("d", {})
        route = self._qq_route(event_type, data)
        workflow_input = {"platform": "qq", "integration_id": config.id, "event_id": event_id, "event_type": event_type, "message_id": data.get("id"), "content": data.get("content", ""), "author": data.get("author", {}), "group_openid": data.get("group_openid"), "channel_id": data.get("channel_id"), "guild_id": data.get("guild_id"), "raw_event": data}
        run = self.workflows.start(project, config.workflow_id, {"input": workflow_input})
        if config.auto_reply and route:
            threading.Thread(target=self._reply_qq_after_run, args=(config, env, run.id, route, str(data.get("id", ""))), daemon=True).start()
        return {"op": 12, "run_id": run.id}

    @classmethod
    def _verify_qq_signature(cls, raw: bytes, headers: dict[str, str], secret: str) -> None:
        timestamp, signature = headers.get("x-signature-timestamp", ""), headers.get("x-signature-ed25519", "")
        if not timestamp or not signature:
            raise PermissionError("QQ 回调缺少签名请求头")
        _require_fresh_timestamp(timestamp)
        try:
            cls._qq_private_key(secret).public_key().verify(bytes.fromhex(signature), timestamp.encode() + raw)
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise PermissionError("QQ 回调签名无效") from exc

    @staticmethod
    def _qq_private_key(secret: str) -> Ed25519PrivateKey:
        secret_bytes = secret.encode()
        seed = (secret_bytes * ((32 // len(secret_bytes)) + 1))[:32] if secret_bytes else b""
        if len(seed) != 32:
            raise RuntimeError("QQ Bot Secret 无效")
        return Ed25519PrivateKey.from_private_bytes(seed)

    @staticmethod
    def _qq_route(event_type: str, data: dict[str, Any]) -> tuple[str, str] | None:
        if event_type == "C2C_MESSAGE_CREATE" and data.get("author", {}).get("user_openid"):
            return "users", str(data["author"]["user_openid"])
        if event_type == "GROUP_AT_MESSAGE_CREATE" and data.get("group_openid"):
            return "groups", str(data["group_openid"])
        if event_type in {"AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"} and data.get("channel_id"):
            return "channels", str(data["channel_id"])
        return None

    def _reply_feishu_after_run(self, config: FeishuIntegrationSpec, env: dict[str, str], run_id: str, message_id: str) -> None:
        text = self._wait_result(run_id)
        if text is None:
            return
        token = self._feishu_token(config, env)
        response = httpx.post(f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply", headers={"Authorization": f"Bearer {token}"}, json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, timeout=30)
        response.raise_for_status()

    def _reply_qq_after_run(self, config: QQIntegrationSpec, env: dict[str, str], run_id: str, route: tuple[str, str], message_id: str) -> None:
        text = self._wait_result(run_id)
        if text is None:
            return
        token = self._qq_token(config, env)
        kind, target = route
        body: dict[str, Any] = {"content": text[:4000], "msg_id": message_id, "msg_seq": 1}
        if kind != "channels":
            body["msg_type"] = 0
        path = f"/channels/{target}/messages" if kind == "channels" else f"/v2/{kind}/{target}/messages"
        response = httpx.post(f"https://api.sgroup.qq.com{path}", headers={"Authorization": f"QQBot {token}", "X-Union-Appid": env[config.app_id_env]}, json=body, timeout=30)
        response.raise_for_status()

    def _wait_result(self, run_id: str) -> str | None:
        run = self.workflows.require(run_id)
        while run.status not in TERMINAL_RUN_STATES:
            time.sleep(.2)
        if run.status != "completed":
            return f"工作流执行失败：{run.error or run.status}"
        output_ids = list(run.outputs)
        value = run.outputs[output_ids[-1]] if output_ids else ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    def _feishu_token(self, config: FeishuIntegrationSpec, env: dict[str, str]) -> str:
        key = f"feishu:{config.id}"
        cached = self._cached_token(key)
        if cached:
            return cached
        response = httpx.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": env[config.app_id_env], "app_secret": env[config.app_secret_env]}, timeout=30)
        response.raise_for_status()
        body = response.json()
        token = str(body.get("tenant_access_token", ""))
        if not token:
            raise RuntimeError(f"飞书获取 tenant token 失败：{body}")
        self._store_token(key, token, int(body.get("expire", 7200)))
        return token

    def _qq_token(self, config: QQIntegrationSpec, env: dict[str, str]) -> str:
        key = f"qq:{config.id}"
        cached = self._cached_token(key)
        if cached:
            return cached
        response = httpx.post("https://bots.qq.com/app/getAppAccessToken", json={"appId": env[config.app_id_env], "clientSecret": env[config.secret_env]}, timeout=30)
        response.raise_for_status()
        body = response.json()
        token = str(body.get("access_token", ""))
        if not token:
            raise RuntimeError(f"QQ 获取 access token 失败：{body}")
        self._store_token(key, token, int(body.get("expires_in", 7200)))
        return token

    def _cached_token(self, key: str) -> str:
        with self._token_lock:
            value = self._tokens.get(key)
            return value[0] if value and value[1] > time.time() + 60 else ""

    def _store_token(self, key: str, token: str, expires_in: int) -> None:
        with self._token_lock:
            self._tokens[key] = (token, time.time() + max(expires_in, 60))


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode(), right.encode())


def _require_fresh_timestamp(value: str, window_seconds: int = 300) -> None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise PermissionError("平台回调时间戳无效") from exc
    if abs(time.time() - timestamp) > window_seconds:
        raise PermissionError("平台回调时间戳已过期")
