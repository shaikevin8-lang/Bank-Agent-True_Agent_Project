"""离线演示用的 OpenAI 兼容 mock 模型网关（纯标准库实现，无第三方依赖）。

职责边界：本服务只负责让 Agent 在**完全离线**的环境里跑通链路，
不实现任何推理能力，也不仿真真实模型的输出质量。

对外接口：

* ``GET  /health``              —— 存活探针，返回服务名与模型名。
* ``GET  /v1/models``           —— OpenAI 兼容的模型列表（只暴露 ``mock-chat``）。
* ``POST /v1/chat/completions`` —— 固定中文应答；``stream: true`` 时改用 SSE 分片推送。

未匹配的路径统一返回 404 JSON ``{"error": "not found"}``。

运行方式：``python server.py``；端口读环境变量 ``MODEL_MOCK_PORT``（默认 ``8010``）。
日志一律写 stderr，方便容器场景下与业务 stdout 分离。

依赖说明：只使用标准库（``os`` / ``json`` / ``logging`` / ``sys`` / ``time`` /
``http.server``），因此镜像构建无需任何 pip install。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 本 mock 对外宣称的模型名；Agent 侧配置填这个名字即可。
MODEL_NAME = "mock-chat"

#: 固定的离线应答文案：明确告知调用方「没有调用真实模型」。
REPLY_TEXT = "已收到请求（model-mock 离线应答，未调用真实模型）。"

#: 流式应答的三个分片；拼起来必须等于 :data:`REPLY_TEXT`。
_STREAM_CHUNKS = ("已收到请求", "（model-mock 离线应答，", "未调用真实模型）。")

#: 监听端口的环境变量名。
_PORT_ENV = "MODEL_MOCK_PORT"

#: 默认监听端口。
_DEFAULT_PORT = "8010"

_LOGGER = logging.getLogger("model-mock")


class _Handler(BaseHTTPRequestHandler):
    """mock 网关的请求处理器，只实现离线演示所需的三个端点。

    方法分发遵循 ``http.server`` 约定（``do_GET`` / ``do_POST``）；
    所有响应体都是 UTF-8 JSON（流式为 ``text/event-stream``）。
    """

    server_version = "model-mock/0.1.0"

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:
        """处理 GET 请求：``/health`` 与 ``/v1/models``，其余 404。"""
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(
                200,
                {"status": "ok", "service": "model-mock", "model": MODEL_NAME},
            )
        elif path == "/v1/models":
            self._send_json(
                200,
                {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]},
            )
        else:
            self._send_json(404, {"error": "not found"})

    # --------------------------------------------------------------- POST
    def do_POST(self) -> None:
        """处理 POST 请求：仅 ``/v1/chat/completions`` 有业务逻辑。

        请求体被解析为 JSON 后：``stream: true`` 走 SSE 分片应答，
        否则返回一次性 OpenAI 兼容 completion 结构。
        """
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return

        payload = self._read_json()
        if payload is None:
            self._send_json(400, {"error": "invalid json"})
            return

        if payload.get("stream") is True:
            self._send_stream()
        else:
            self._send_json(200, self._completion_body())

    # ------------------------------------------------------------- 内部工具
    def _read_json(self) -> dict | None:
        """读取并解析请求体 JSON。

        Returns:
            解析成功且顶层为对象时返回该 dict；
            请求体不是合法 JSON（或顶层不是对象）时返回 ``None``。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _completion_body() -> dict:
        """构造非流式 OpenAI 兼容应答体（键集固定）。

        Returns:
            含 ``id`` / ``object`` / ``created`` / ``model`` / ``choices`` / ``usage``
            的 dict；``usage`` 全为 0，因为 mock 并未真正推理。
        """
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": REPLY_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _send_json(self, status: int, body: dict) -> None:
        """输出一个 JSON 响应。

        Args:
            status: HTTP 状态码。
            body: 将被 ``json.dumps`` 序列化（``ensure_ascii=False``）的响应体。
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self) -> None:
        """输出 ``text/event-stream`` 流式应答：3 个内容分片 + 结束帧 + ``[DONE]``。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        for piece in _STREAM_CHUNKS:
            self._write_event(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": MODEL_NAME,
                    "choices": [
                        {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                    ],
                }
            )
        # 收尾帧：空 delta + finish_reason=stop，符合 OpenAI 流式语义。
        self._write_event(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_NAME,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _write_event(self, event: dict) -> None:
        """写出一帧 SSE ``data: {...}`` 并立刻 flush。

        Args:
            event: 待序列化的帧内容（JSON 对象）。
        """
        line = json.dumps(event, ensure_ascii=False).encode("utf-8")
        self.wfile.write(b"data: " + line + b"\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 名称沿用 http.server 签名
        """把 ``http.server`` 的访问日志统一交给 logging（最终写 stderr）。

        Args:
            format: printf 风格格式串（由标准库传入）。
            *args: 格式串参数。
        """
        _LOGGER.info("%s - %s", self.address_string(), format % args)


def main() -> int:
    """启动 mock 网关并阻塞服务，直到 Ctrl+C 中断。

    Returns:
        进程退出码：正常退出为 ``0``。
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get(_PORT_ENV, _DEFAULT_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    _LOGGER.info(
        "model-mock 已启动：http://0.0.0.0:%d（模型 %s，端口来源 %s）",
        port,
        MODEL_NAME,
        _PORT_ENV,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("收到中断信号，正在退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
