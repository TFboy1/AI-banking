"""运行：uv run --extra feishu python -m banking.feishu_bridge。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx

from .config import Settings
from .db import dumps


def forward(settings, payload):
    """A response timeout is an unknown acknowledgement, not service failure.

    The backend may already have committed this event and queued its task.
    Never retry here or tell the user that no operation happened; platform
    redelivery retains event_id and is deduplicated by the bank.
    """
    timeout = httpx.Timeout(2) if payload.get('kind') == 'card' else httpx.Timeout(connect=2, read=15, write=5, pool=2)
    try:
        response = httpx.post(
            settings.backend_url.rstrip('/') + '/api/internal/feishu',
            json=payload,
            headers={'Authorization': 'Bearer ' + settings.feishu_bridge_token},
            timeout=timeout,
            trust_env=False,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Expected an acknowledgement object')
        return result
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return {'reply': '暂时连接不上本地银行服务，请稍后重试或检查后端是否启动。'}
    except (httpx.TimeoutException, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
        return {'reply': '暂未取得接收回执，请稍候查看后续结果，勿重复提交。交易是否完成以执行回执为准。'}
    except (httpx.HTTPError, ValueError):
        return {'reply': '后台响应异常，暂未确认请求状态。请稍候核对后续结果，勿重复提交。'}


def main():
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    settings = Settings()
    if not settings.feishu_ready:
        raise SystemExit("请先在 .env 中启用飞书，填写应用凭证和至少 32 字符的 FEISHU_BRIDGE_TOKEN。")
    client = (
        lark.Client.builder().app_id(settings.feishu_app_id).app_secret(settings.feishu_app_secret).build()
    )
    acknowledgements = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-ack")

    def on_message(data):
        raw = json.loads(lark.JSON.marshal(data))
        event, header = raw["event"], raw["header"]
        message = event["message"]
        sender = event["sender"]
        if message.get("chat_type") != "p2p" or sender.get("sender_type") != "user":
            return
        content = json.loads(message.get("content", "{}"))
        result = forward(settings,
            {
                "kind": "message",
                "event_id": "msg:" + header["event_id"],
                "tenant_key": header["tenant_key"],
                "open_id": sender["sender_id"]["open_id"],
                "chat_id": message["chat_id"],
                "chat_type": "p2p",
                "text": content.get("text", "")[:4000] if message.get("message_type") == "text" else "",
            }
        )
        if result.get("reply") and not result.get("duplicate"):
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(message["chat_id"])
                    .msg_type("text")
                    .content(dumps({"text": result["reply"]}))
                    .uuid("ack_" + hashlib.sha256(header["event_id"].encode("utf-8")).hexdigest()[:32])
                    .build()
                )
                .build()
            )
            # 入站消息已在后端落盘；发回提示放入独立线程，避免拖慢事件确认。
            acknowledgements.submit(client.im.v1.message.create, request)

    def on_card(data):
        raw = json.loads(lark.JSON.marshal(data))
        event, header = raw["event"], raw["header"]
        operator = event["operator"]
        value = event.get("action", {}).get("value", {})
        result = forward(settings,
            {
                "kind": "card",
                "event_id": "card:" + (header.get("event_id") or event["token"]),
                "tenant_key": operator.get("tenant_key") or header["tenant_key"],
                "open_id": operator["open_id"],
                "chat_id": event["context"]["open_chat_id"],
                "chat_type": "p2p",
                "approval_id": value.get("approval_id"),
                "decision": value.get("decision"),
                **{key:value[key] for key in ('action','task_id','conversation_id','message_id','choice','version','order_id','half_stars') if key in value},
            }
        )
        return P2CardActionTriggerResponse(
            {"toast": {"type": "info", "content": result.get("reply", "已处理")}}
        )

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .build()
    )
    print("飞书私聊桥接已启动，正在保持长连接。")
    try:
        lark.ws.Client(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        ).start()
    finally:
        acknowledgements.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
