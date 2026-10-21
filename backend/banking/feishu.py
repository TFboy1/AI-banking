"""可选的飞书消息输出；只在配置启用后运行，不访问用户通讯录。"""

import asyncio
import json
import time

import httpx

from .db import dumps


def card_for_message(message, payment_url=None, money_operation=False):
    data = message["data"]
    elements = []
    title = "有数 · 银行助手"
    color = "green"
    def button(label,value):
        return {'tag':'button','text':{'tag':'plain_text','content':label},'type':'default','value':value}
    def paragraph(value):
        elements.append({'tag':'div','text':{'tag':'plain_text','content':str(value)[:12000]}})
    def actions(values):
        for index in range(0,len(values),5):elements.append({'tag':'action','actions':values[index:index+5]})
    if message['kind']=='feishu_task':
        title='有数 · 任务进度'
        status=data['status'];active=status in {'queued','running','waiting_confirmation'}
        paragraph(({'queued':'等待处理','running':'处理中','waiting_confirmation':'等待确认','completed':'已完成','failed':'已停止或失败','cancelled':'已取消'}.get(status,status))+' · '+data['stage'])
        value={'task_id':data['task_id'],'conversation_id':data['conversation_id']}
        if active:actions([button('停止任务',{'action':'stop',**value})])
        elif status in {'failed','cancelled'}:actions([button('核对并继续',{'action':'resume',**value})])
    elif data.get('handoff_rating') is not None:
        title='人工服务评价';score=data['handoff_rating'].get('half_stars')
        paragraph(f'当前评分：{score/2:g}星' if score else '请为本次人工服务评分，支持半星。')
        actions([button(f'★ {i/2:g}',{'action':'rating','message_id':message['id'],'conversation_id':message['conversation_id'],'half_stars':i}) for i in range(1,11)])
    elif data.get('human_session_id'):
        title='有数 · 人工客服';paragraph(message['text'])
        if '已结束' not in message['text']:
            actions([button('结束人工服务',{'action':'human_end','message_id':message['id'],'conversation_id':message['conversation_id']})])
    elif message["kind"] == "approval":
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
                            "text": {"tag": "plain_text", "content": "前往验证并支付" if payment_url else "请到有数验证" if money_operation else "确认操作"},
                            "type": "primary",
                            **({"url": payment_url} if payment_url else
                               {"value": {"approval_id": data["approval_id"], "decision": "confirm"}}),
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
        partial=result.get('partial_completion')
        daily=result.get('daily_digest')
        if partial:
            title='部分操作已完成 · 后续处理'
            from .bank import yuan
            paragraph(partial['reason']+'\n已扣款 '+yuan(partial['debit_minor'])+' · 已到账 '+yuan(partial['credit_minor']))
            for item in partial['completed']:paragraph(item['title']+' · '+item['settlement'])
            paragraph(partial['note'])
            value={'action':'partial','task_id':partial['task_id'],'conversation_id':message['conversation_id'],'version':partial['version']}
            choices=[]
            if partial['decision']=='pending':
                choices.append(button('保留完成项目',dict(value,choice='keep')))
                if partial['can_replan']:choices.append(button('重选未完成项目',dict(value,choice='replan')))
                choices.extend(button('退订：'+item['title'],dict(value,choice='cancel_order',order_id=item['order_id'])) for item in partial['completed'] if item['can_cancel'])
            actions(choices)
            if partial['decision']!='pending':paragraph('处理状态：'+partial['decision'])
            display={'title':title,'columns':[],'rows':[]}
        elif daily:
            title=daily['greeting'];paragraph(daily['subtitle'])
            for section in daily['sections']:
                paragraph(section['title']+'\n'+'\n'.join(m['label']+'：'+m['value'] for m in section.get('metrics',[]))+'\n'+'\n'.join(' | '.join(map(str,row)) for row in section.get('rows',[])[:12])+'\n'+section.get('note',''))
            display={'title':title,'columns':[],'rows':[],'note':'完整图表及明细请在有数的日报会话查看。'}
        else:display = result.get("display") or {"title": message.get('text') or '有数业务结果',
            "columns": [], "rows": [], "note": '该结果包含详细内容，请在有数原会话中查看完整报告与后续处理选项。'}
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
    if data.get('_feishu_notify'):
        actions([button('在此继续处理',{'action':'focus','message_id':message['id'],'conversation_id':message['conversation_id']})])
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
                            await asyncio.wait_for(self.changed.wait(), timeout=5)
                        except TimeoutError:
                            pass
                        continue
                try:
                    message = json.loads(pending['payload_json'])
                    destination=message.get('_feishu_recipient')
                    from .feishu_channels import recipient
                    with self.db.connection() as db:current=recipient(db)
                    if destination!=current:
                        with self.db.transaction() as db:
                            db.execute("UPDATE feishu_outbox SET status='failed',error='飞书绑定已变化，旧消息不再投递' WHERE message_id=?",(pending['message_id'],))
                        continue
                    token = await self._access_token(client)
                    payment_url = None
                    money_operation = False
                    if message['kind'] == 'approval' and message['data']['status'] == 'pending':
                        from .security import MONEY_TOOLS
                        from .feishu_payment import payment_link
                        with self.db.connection() as db:
                            approval = db.execute('SELECT tool_name FROM approvals WHERE id=?', (message['data']['approval_id'],)).fetchone()
                            money_operation = bool(approval and approval['tool_name'] in MONEY_TOOLS)
                            if money_operation:
                                payment_url = payment_link(db, message['data']['approval_id'], self.settings)
                    receipt = message['kind'] == 'feishu_receipt'
                    content = dumps({'text': message['text']} if receipt else card_for_message(message, payment_url, money_operation))
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
                                "msg_type": "text" if receipt else "interactive",
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
