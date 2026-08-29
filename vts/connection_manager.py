"""
VTube Studio WebSocket 连接管理
- VTSConnectionManager：连接/鉴权/重连/口型/表情/热键指令封装
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from threading import Lock, Thread

import websocket as ws

logger = logging.getLogger(__name__)


class VTSConnectionManager:
    def __init__(self, ws_url: str, token_file: str = None):
        self.ws_url = ws_url
        self.ws = None
        self.ws_lock = Lock()
        self.connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.message_buffer = []
        self.auth_token = None
        self.ws_app = None
        # 重连后的回调（如 ExpressionController 恢复表情状态）
        self.on_reconnect_callback = None
        # Deprecated compatibility observer. Mainline local rendering is owned
        # by tts.mouth_signal.MouthSignalRouter; this remains for older callers.
        self.on_mouth_value: callable | None = None
        try:
            from config.settings import VTS_RECONNECT_ENABLED
            self.reconnect_enabled = bool(VTS_RECONNECT_ENABLED)
        except Exception:
            self.reconnect_enabled = True

        # 后台重连线程控制（保证只有一个重连线程在跑）
        self._reconnect_lock = Lock()
        self._reconnect_running = False

        # token 文件路径，默认从 config.settings 读取
        if token_file is None:
            try:
                from config.settings import VTS_TOKEN_FILE
                token_file = VTS_TOKEN_FILE
            except Exception:
                token_file = "vts_token.json"
        self._token_file = token_file

    # ------------------------------------------------------------------
    # Token 持久化
    # ------------------------------------------------------------------
    def _load_auth_token(self) -> str | None:
        if os.path.exists(self._token_file):
            try:
                with open(self._token_file, "r") as f:
                    token_data = json.load(f)
                    self.auth_token = token_data.get("authenticationToken")
                    return self.auth_token
            except Exception as e:
                logger.error(f"reading token file failed: {e}")
        return None

    def _save_auth_token(self, token: str) -> None:
        try:
            with open(self._token_file, "w") as f:
                json.dump({"authenticationToken": token}, f)
            logger.info("VTS token saved to file")
        except Exception as e:
            logger.error(f"saving token failed: {e}")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if self.connected and self.ws and self.ws.connected:
            return True
        _is_reconnect = self.reconnect_attempts > 0
        self.disconnect()
        try:
            self.ws = ws.WebSocket()
            self.ws.settimeout(10)  # 仅用于建连阶段
            self.ws.connect(
                self.ws_url,
                skip_utf8_validation=True,
                suppress_origin=True,
                enable_multithread=True,
            )
            self.ws.settimeout(None)  # 建连成功后取消全局超时，避免误判断联
            logger.info("VTS WebSocket connected")
            self.connected = True
            self.reconnect_attempts = 0

            if not self.auth_token:
                self.auth_token = self._load_auth_token()

            if self.auth_token:
                self.authenticate()
            else:
                self.request_auth_token()

            self._send_buffered_messages()

            # 重连后通知上层恢复表情状态
            if _is_reconnect and self.on_reconnect_callback is not None:
                try:
                    logger.info("VTS reconnected; restoring expression state")
                    self.on_reconnect_callback()
                except Exception as cb_err:
                    logger.warning(f"on_reconnect_callback failed: {cb_err}")

            return True
        except Exception as e:
            self._handle_connection_error(e)
            return False

    def disconnect(self) -> None:
        with self.ws_lock:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
            self.connected = False

    def _handle_connection_error(self, error) -> None:
        self.connected = False
        if not self.reconnect_enabled:
            logger.warning(f"VTS connection error: {error}; background reconnect is disabled")
            return
        logger.warning(f"VTS connection error: {error}; starting background reconnect")
        self._start_background_reconnect()

    def _start_background_reconnect(self) -> None:
        """确保只有一个后台重连线程在运行。"""
        if not self.reconnect_enabled:
            return
        with self._reconnect_lock:
            if self._reconnect_running:
                return
            self._reconnect_running = True
        Thread(target=self._reconnect_loop, daemon=True, name="vts-reconnect").start()

    def _reconnect_loop(self) -> None:
        """后台重连循环，指数退避，sleep 只在此线程中发生，不阻塞任何调用方。"""
        try:
            while not self.connected:
                if self.reconnect_attempts >= self.max_reconnect_attempts:
                    logger.error(f"maximum reconnect attempts reached ({self.max_reconnect_attempts}); giving up")
                    break
                backoff = min(0.5 * (2 ** self.reconnect_attempts) + random.uniform(0, 1), 30)
                self.reconnect_attempts += 1
                logger.warning(
                    f"VTS reconnect attempt in {backoff:.2f}s "
                    f"({self.reconnect_attempts}/{self.max_reconnect_attempts})"
                )
                time.sleep(backoff)
                self.connect()
        finally:
            with self._reconnect_lock:
                self._reconnect_running = False

    # ------------------------------------------------------------------
    # 消息收发
    # ------------------------------------------------------------------
    def send_message(self, payload: dict, bufferable: bool = True) -> bool:
        """发送消息。断联时立即返回 False，不阻塞调用方。
        bufferable=False 用于高频 fire-and-forget 消息（如口型数据），断联时直接丢弃。
        """
        if not self.connected or not self.ws:
            if bufferable:
                self.message_buffer.append(payload)
                self._start_background_reconnect()
            return False
        try:
            with self.ws_lock:
                self.ws.send(json.dumps(payload))
            return True
        except Exception as e:
            logger.warning(f"message send failed: {e}")
            if bufferable:
                self.message_buffer.append(payload)
            self._handle_connection_error(e)
            return False

    def send_ping(self) -> None:
        """发送 WebSocket 协议级 PING 帧，保持 TCP 连接活跃。"""
        if not self.connected or not self.ws:
            return
        try:
            with self.ws_lock:
                self.ws.ping()
        except Exception as e:
            logger.warning(f"PING send failed: {e}")
            self._handle_connection_error(e)

    def _send_buffered_messages(self) -> None:
        if not self.message_buffer:
            return
        logger.info(f"sending buffered messages: count={len(self.message_buffer)}")
        messages_to_send = self.message_buffer.copy()
        self.message_buffer.clear()
        for msg in messages_to_send:
            self.send_message(msg)

    def receive_message(self, timeout: float = 1) -> dict | None:
        if not self.connected or not self.ws:
            return None
        try:
            with self.ws_lock:
                self.ws.settimeout(timeout)
                return json.loads(self.ws.recv())
        except ws.WebSocketTimeoutException:
            return None
        except Exception as e:
            logger.warning(f"message receive failed: {e}")
            self._handle_connection_error(e)
            return None

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    def request_auth_token(self) -> str | None:
        logger.info("requesting new VTS auth token")
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "get_token",
            "messageType": "AuthenticationTokenRequest",
            "data": {"pluginName": "Lucas-Plugin", "pluginDeveloper": "Lucas"},
        }
        if self.send_message(payload):
            for _ in range(10):
                response = self.receive_message(timeout=0.5)
                if response and response.get("messageType") == "AuthenticationTokenResponse":
                    token = response.get("data", {}).get("authenticationToken")
                    if token:
                        self.auth_token = token
                        self._save_auth_token(token)
                        logger.info("VTS token request succeeded")
                        return token
            logger.error("VTS token response timed out or was invalid")
        return None

    def authenticate(self) -> bool:
        if not self.auth_token:
            logger.warning("no VTS auth token is available")
            return False
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "auth_lucas",
            "messageType": "AuthenticationRequest",
            "data": {
                "pluginName": "Lucas-Plugin",
                "pluginDeveloper": "Lucas",
                "authenticationToken": self.auth_token,
            },
        }
        if self.send_message(payload):
            logger.info("VTS authentication request sent")
            for _ in range(5):
                response = self.receive_message(timeout=0.5)
                if response and response.get("messageType") == "AuthenticationResponse":
                    if response.get("data", {}).get("authenticated", False):
                        logger.info("VTS authentication succeeded")
                        return True
                    else:
                        logger.warning("VTS authentication failed; requesting a new token")
                        self.auth_token = None
                        return self.request_auth_token() is not None
            logger.warning("VTS authentication response timed out")
        return False

    # ------------------------------------------------------------------
    # 控制指令
    # ------------------------------------------------------------------
    def send_mouth_data(self, mouth_value: float) -> bool:
        """高频口型数据，断联时直接丢弃，绝不阻塞播放线程。"""
        # 路由到渲染引擎（如已绑定）
        if self.on_mouth_value is not None:
            try:
                self.on_mouth_value(mouth_value)
            except Exception:
                pass
        if not self.connected or not self.ws:
            return False
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "mouth_sync",
            "messageType": "InjectParameterDataRequest",
            "data": {"parameterValues": [{"id": "MouthOpen", "value": mouth_value}]},
        }
        return self.send_message(payload, bufferable=False)

    def send_parameters(self, param_dict: dict) -> bool:
        """高频参数注入，断联时直接丢弃，不阻塞调用方。"""
        if not self.connected or not self.ws:
            return False
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "param_batch",
            "messageType": "InjectParameterDataRequest",
            "data": {
                "parameterValues": [
                    {"id": pid, "value": float(val)} for pid, val in param_dict.items()
                ]
            },
        }
        return self.send_message(payload, bufferable=False)

    def trigger_hotkey(self, name_or_id: str) -> bool:
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "hotkey_trigger",
            "messageType": "HotkeyTriggerRequest",
            "data": {"hotkeyID": name_or_id, "hotkeyName": name_or_id},
        }
        return self.send_message(payload)

    def activate_expression(
        self,
        name_or_file: str,
        active: bool = True,
        fade_time: float = 0.0,
        weight: float = None,
    ) -> bool:
        data: dict = {"active": bool(active), "fadeTime": float(fade_time)}
        if name_or_file.lower().endswith(".exp3.json"):
            data["expressionFile"] = name_or_file
        else:
            data["expressionName"] = name_or_file
        if weight is not None:
            try:
                data["weight"] = float(weight)
            except Exception:
                pass
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "expr_activate",
            "messageType": "ExpressionActivationRequest",
            "data": data,
        }
        logger.info(f"sending expression activation request: {payload}")
        ok = self.send_message(payload)
        if ok and self.connected:
            # 读取响应仅用于记录错误；失败时静默忽略，不触发二次重连
            try:
                with self.ws_lock:
                    self.ws.settimeout(0.5)
                    raw = self.ws.recv()
                resp = json.loads(raw)
                err = (resp.get("data") or {}).get("errorID")
                msg = (resp.get("data") or {}).get("message", "")
                if err:
                    logger.warning(f"[VTS] expression request error errorID={err}: {msg}")
                else:
                    logger.debug(f"[VTS] expression request succeeded: {resp.get('messageType')}")
            except Exception:
                pass  # 连接不稳定时忽略响应读取，send_message 已负责重连
        return ok

    def send_heartbeat(self) -> bool:
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "heartbeat",
            "messageType": "APIStateRequest",
            "data": {},
        }
        ok = self.send_message(payload)
        if ok:
            # 读掉 VTS 返回的 APIStateResponse，防止缓冲区堆积导致 VTS 主动断联
            try:
                with self.ws_lock:
                    self.ws.settimeout(0.3)
                    self.ws.recv()
                    self.ws.settimeout(None)
            except Exception:
                pass
        return ok
