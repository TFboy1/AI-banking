import json

import httpx
import pytest
from banking.llm import ModelClient, ModelError


def configure(settings):
    settings.llm_base_url = "https://model.example/v1"
    settings.llm_api_key = "test-api-secret"
    settings.llm_model = "test-model"


async def test_compatible_tool_call_protocol(settings, monkeypatch):
    configure(settings)
    real_client = httpx.AsyncClient

    def handle(request):
        import json

        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-api-secret"
        assert body["model"] == "test-model" and body["tool_choice"] == "auto"
        assert any(t["function"]["name"] == "transfer" for t in body["tools"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "内部过程不会暴露",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_accounts", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    result = await ModelClient(settings).complete([{"role": "user", "content": "查询余额"}])
    assert result["tool_calls"][0]["function"]["name"] == "get_accounts"
    assert "reasoning_content" not in result


@pytest.mark.parametrize("status,body", [(401, {"detail": "test-api-secret"}), (200, {"broken": True})])
async def test_upstream_errors_do_not_expose_credentials(settings, monkeypatch, status, body):
    configure(settings)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)), **kwargs
        ),
    )
    with pytest.raises(ModelError) as error:
        await ModelClient(settings).complete([])
    assert "test-api-secret" not in str(error.value)


async def test_plaintext_without_tool_evidence_uses_structured_adapter(settings, monkeypatch):
    configure(settings)
    requests = []
    responses = iter(
        [
            "这个月消费6000元。",
            json.dumps({"type": "tool_calls", "calls": [{"name": "analyze_bills", "arguments": {}}]}),
            json.dumps({"type": "reply", "reason": "summary", "text": "本月支出为7628.50元。"}),
        ]
    )
    real_client = httpx.AsyncClient

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": next(responses)}}
                ]
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    client = ModelClient(settings)
    messages = [{"role": "system", "content": "银行助手"}, {"role": "user", "content": "这个月的钱花在哪了"}]
    result = await client.complete(messages)
    assert result["tool_calls"][0]["function"]["name"] == "analyze_bills"
    assert result["content"] == ""
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[1]
    # 后续工具结果进入同一协议，不能把数据库回执丢掉或再次当成用户指令。
    # 服务重启产生新客户端时，仍从持久化调用记录恢复结构化协议。
    reply = await ModelClient(settings).complete(
        messages
        + [
            result,
            {
                "role": "tool",
                "tool_call_id": result["tool_calls"][0]["id"],
                "content": '{"expense_minor":762850}',
            },
        ]
    )
    assert "7628.50" in reply["content"]
    assert "银行工具结果" in requests[2]["messages"][-1]["content"]
    assert "6000" not in str(requests[1:])
    assert messages[0]["content"] == "银行助手"


@pytest.mark.parametrize(
    "plan",
    [
        {"type": "reply", "reason": "summary", "text": "本月消费6000元"},
        {"type": "tool_calls", "calls": []},
        {"type": "tool_calls", "calls": [{"name": "transfer", "arguments": []}]},
        {"type": "tool_calls", "calls": [{"name": "transfer", "arguments": {}, "confirmed": True}]},
        {"type": "reply", "reason": "unknown", "text": "完成"},
    ],
)
async def test_structured_adapter_rejects_invalid_plans_and_ungrounded_summary(settings, monkeypatch, plan):
    configure(settings)
    settings.llm_tool_mode = "structured"
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"choices": [{"message": {"content": json.dumps(plan)}}]}
                )
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ModelError):
        await ModelClient(settings).complete([{"role": "user", "content": "这个月的钱花在哪了"}])


async def test_structured_format_retry_keeps_tool_result_and_never_repeats_tool(settings, monkeypatch):
    configure(settings)
    settings.llm_tool_mode = "structured"
    replies = iter(
        ["日常账户有钱。", json.dumps({"type": "reply", "reason": "summary", "text": "账户余额见上方表格。"})]
    )
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    result = await ModelClient(settings).complete(
        [
            {"role": "user", "content": "查询我的余额"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_json_1", "function": {"name": "get_accounts", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_json_1", "content": '{"balance_minor":1286050}'},
        ]
    )
    assert "表格" in result["content"] and not result.get("tool_calls")
    assert len(requests) == 2
    assert "服务端格式校验" in requests[1]["messages"][-1]["content"]
    assert sum("银行工具结果" in m["content"] for m in requests[1]["messages"]) == 1
