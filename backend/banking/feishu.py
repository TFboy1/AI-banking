"""可选的飞书消息输出；只在配置启用后运行，不访问用户通讯录。"""

import asyncio
import json
import time

import httpx

from .db import dumps


def card_for_message(message):
    data = message["data"]
    elements = []
    title = "有数 · 银行助手"
    color = "green"
    if message["kind"] == "approval":
        title = data["title"]
        status = data["status"]
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": "\n".join(f"{f['label']}：{f['value']}" for f in data["fields"]),
                },
            }
        )
        if status == "pending":
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "确认操作"},
                            "type": "primary",
                            "value": {"approval_id": data["approval_id"], "decision": "confirm"},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "取消"},
                            "type": "default",
                            "value": {"approval_id": data["approval_id"], "decision": "cancel"},
                        },
                    ],
                }
            )
        else:
            label = {
                "executed": "已执行",
                "cancelled": "已取消",
                "failed": "未执行",
                "expired": "已过期",
            }.get(status, status)
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": label + (" · " + data["error"] if data.get("error") else ""),
                    },
                }
            )
            if data.get("receipt"):
                elements.append(
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": "回执：" + data["receipt"]["action_id"]}
                        ],
                    }
                )
    elif message["kind"] == "data":
        result = data["result"]
        display = result["display"]
        title = display["title"]
        lines = [f"{m['label']}：{m['value']}" for m in display.get("metrics", [])]
        lines += [" | ".join(display["columns"])]
        lines += [" | ".join(str(value) for value in row) for row in display["rows"]]
        lines += [a["text"] for a in result.get("alerts", [])]
        if display.get("note"):
            lines.append(display["note"])
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)[:12000]}})
    else:
        color = "orange" if message["kind"] == "error" else "green"
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": message["text"][:12000]}})
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "个人演示沙箱 · 所有资金与交易均为模拟"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


class OutboxSender:
    def __init__(self, database, settings, condition):
        self.db, self.settings, self.changed = database, settings, condition
        self.token = ""
        self.token_expires = 0

    async def _access_token(self, client):
        if self.token and time.monotonic() < self.token_expires:
            return self.token
        response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.settings.feishu_app_id, "app_secret": self.settings.feishu_app_secret},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError("飞书应用凭证或权限不可用")
        self.token = body["tenant_access_token"]
        self.token_expires = time.monotonic() + max(30, body.get("expire", 7200) - 120)
        return self.token

    async def run(self):
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                async with self.changed:
                    with self.db.connection() as db:
                        pending = db.execute(
                            "SELECT * FROM feishu_outbox WHERE status='pending' ORDER BY rowid LIMIT 1"
                        ).fetchone()
                        binding = db.execute("SELECT * FROM feishu_binding WHERE id=1").fetchone()
                    if not pending or not binding:
                        try:
                            await asyncio.wait_for(self.changed.wait(), timeout=30)
                        except TimeoutError:
                            pass
                        continue
                try:
                    token = await self._access_token(client)
                    content = dumps(card_for_message(json.loads(pending["payload_json"])))
                    headers = {"Authorization": "Bearer " + token}
                    if pending["external_id"]:
                        response = await client.patch(
                            "https://open.feishu.cn/open-apis/im/v1/messages/" + pending["external_id"],
                            headers=headers,
                            json={"content": content},
                        )
                    else:
                        response = await client.post(
                            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                            headers=headers,
                            json={
                                "receive_id": binding["chat_id"],
                                "msg_type": "interactive",
                                "content": content,
                                "uuid": pending["message_id"],
                            },
                        )
                    response.raise_for_status()
                    result = response.json()
                    if result.get("code") != 0:
                        raise RuntimeError("飞书消息发送或更新失败")
                    external_id = pending["external_id"] or result["data"]["message_id"]
                    with self.db.transaction() as db:
                        # 发送期间卡片可能更新：只把当前版本标记已发送，保留新版本待更新。
                        db.execute(
                            "UPDATE feishu_outbox SET external_id=?,status=CASE WHEN payload_json=? THEN 'sent' ELSE 'pending' END,error=NULL WHERE message_id=?",
                            (external_id, pending["payload_json"], pending["message_id"]),
                        )
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE feishu_outbox SET attempts=attempts+1,status=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,error='飞书发送失败，请检查网络与应用权限' WHERE message_id=?",
                            (pending["message_id"],),
                        )
                    await asyncio.sleep(min(30, 2 ** (pending["attempts"] + 1)))
