import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from banking.feishu_bridge import forward


def test_slow_ack_does_not_report_unavailable_or_resubmit(settings):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            time.sleep(2.2)  # Old bridge failed at two seconds after acceptance.
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"task_id":"accepted-once","reply":"accepted"}')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        settings.backend_url=f'http://127.0.0.1:{server.server_port}'
        settings.feishu_bridge_token='isolated-test-token'
        payload={'kind':'message','event_id':'same-original-event'}
        assert forward(settings,payload)=={'task_id':'accepted-once','reply':'accepted'}
        assert calls==[payload]
    finally:server.shutdown();server.server_close();worker.join()


@pytest.mark.parametrize('error',[httpx.ReadTimeout,httpx.WriteTimeout,httpx.ReadError,httpx.WriteError,httpx.RemoteProtocolError])
def test_unknown_ack_never_claims_failure_or_retries(settings,monkeypatch,error):
    calls=[]
    def post(*args,**kwargs):calls.append(kwargs);raise error('simulated')
    monkeypatch.setattr('banking.feishu_bridge.httpx.post',post)
    result=forward(settings,{'kind':'message','event_id':'event'})
    assert '暂未取得接收回执' in result['reply'] and '不可用' not in result['reply']
    assert len(calls)==1 and calls[0]['timeout'].read==15 and calls[0]['trust_env'] is False


@pytest.mark.parametrize('error',[httpx.ConnectError,httpx.ConnectTimeout])
def test_actual_connection_failure_is_distinguished(settings,monkeypatch,error):
    def post(*args,**kwargs):raise error('simulated')
    monkeypatch.setattr('banking.feishu_bridge.httpx.post',post)
    assert '连接不上' in forward(settings,{'kind':'message'})['reply']


def test_card_timeout_budget_and_duplicate_result_preserved(settings,monkeypatch):
    def post(*args,**kwargs):
        assert kwargs['timeout'].read==2
        return httpx.Response(200,json={'reply':'done','duplicate':True},request=httpx.Request('POST',args[0]))
    monkeypatch.setattr('banking.feishu_bridge.httpx.post',post)
    assert forward(settings,{'kind':'card'})=={'reply':'done','duplicate':True}


@pytest.mark.parametrize('status,data',[(500,{}),(403,{}),(200,[]),(200,None)])
def test_bad_http_or_json_response_does_not_claim_transaction_failed(settings,monkeypatch,status,data):
    def post(*args,**kwargs):return httpx.Response(status,json=data,request=httpx.Request('POST',args[0]))
    monkeypatch.setattr('banking.feishu_bridge.httpx.post',post)
    assert '暂未确认请求状态' in forward(settings,{'kind':'message'})['reply']
