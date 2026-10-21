"""使用临时数据库和本机端口检查后端 HTTP/SSE；不会打开浏览器。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def verify(base_url):
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        response = await client.get("/api/health")
        response.raise_for_status()
        assert response.json()["status"] == "ok"
        assert (await client.get("/api/bootstrap")).status_code == 401
        await client.post("/api/auth/open", json={})
        sms = await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "register"})
        sms.raise_for_status()
        registration = await client.post("/api/auth/login", json={
            "phone": "13800123456", "purpose": "register", "username": "HTTP检查用户", "code": sms.json()["demo_code"],
        })
        registration.raise_for_status()
        (await client.post("/api/auth/contacts-done", json={})).raise_for_status()
        (await client.post("/api/auth/pin", json={"pin": "2468", "repeat_pin": "2468"})).raise_for_status()
        initial = (await client.get("/api/bootstrap")).json()
        queried = await client.post("/api/sandbox/inspect", json={"tool": "analyze_bills"})
        queried.raise_for_status()
        submitted = await client.post("/api/messages", json={"text": "查询我的余额"})
        submitted.raise_for_status()
        task_id = submitted.json()["task_id"]
        received = []
        current_id = None
        event_type = ""
        async with asyncio.timeout(15):
            async with client.stream("GET", f"/api/events?after={initial['event_id']}") as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        current_id = int(line[4:])
                    elif line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        received.append((current_id, event_type, data))
                        if event_type == "task" and data["id"] == task_id and data["status"] == "failed":
                            break
        assert any(kind == "message" and data["kind"] == "data" for _, kind, data in received)
        assert any(
            kind == "message" and data["kind"] == "error" and "尚未连接大模型" in data["text"]
            for _, kind, data in received
        )
        ids = [row[0] for row in received]
        assert ids == sorted(set(ids))
        # Last-Event-ID 应优先于 URL 参数，且只重放后续持久化事件。
        async with asyncio.timeout(10):
            async with client.stream(
                "GET", "/api/events?after=99999999", headers={"Last-Event-ID": str(ids[0])}
            ) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        assert int(line[4:]) == ids[1]
                        break
        after = (await client.get("/api/bank")).json()
        assert after["total_assets_minor"] == initial["bank"]["total_assets_minor"]
        # 即使浏览器最后选择的会话变化，显式订阅只收到该会话消息和全局状态事件。
        first_id = initial["conversation_id"]
        second_id = (await client.post("/api/conversations", json={})).json()["conversation_id"]
        second = (await client.get("/api/bootstrap")).json()
        assert second["conversation_id"] == second_id
        await client.post("/api/sandbox/inspect", json={"tool": "get_accounts", "conversation_id": first_id})
        own = (
            await client.post(
                "/api/sandbox/inspect", json={"tool": "list_cards", "conversation_id": second_id}
            )
        ).json()
        async with asyncio.timeout(10):
            async with client.stream(
                "GET", f"/api/events?after={second['event_id']}&conversation_id={second_id}"
            ) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        if event_type == "message":
                            assert data["conversation_id"] == second_id
                            assert data["id"] == own["id"]
                            break
        deleted = await client.delete(f"/api/conversations/{first_id}")
        deleted.raise_for_status()
        assert (await client.get("/api/bootstrap")).json()["conversation_id"] == second_id
        print(f"HTTP/SSE 检查通过：只读查询、模型未配置提示、{len(received)} 条事件、断线重放与多会话隔离。")


def main():
    data_root = (ROOT / "data").resolve()
    data_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke_", dir=data_root) as temporary:
        # 限定自动清理范围为本项目 data 下本次生成的临时目录。
        temporary_path = Path(temporary).resolve()
        assert temporary_path.parent == data_root
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {
            **os.environ,
            "PYTHONUTF8": "1",
            "APP_AUTH_ENABLED": "true",
            "DATABASE_PATH": str(temporary_path / "test.sqlite3"),
            "LLM_BASE_URL": "",
            "LLM_API_KEY": "",
            "LLM_MODEL": "",
            "FEISHU_ENABLED": "false",
        }
        ready = threading.Event()
        output = []
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "banking.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def read_logs():
            for line in process.stdout:
                output.append(line)
                if "Application startup complete" in line:
                    ready.set()

        threading.Thread(target=read_logs, daemon=True).start()
        try:
            if not ready.wait(20):
                raise RuntimeError("临时后端启动失败：" + "".join(output[-8:]))
            asyncio.run(verify(f"http://127.0.0.1:{port}"))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
