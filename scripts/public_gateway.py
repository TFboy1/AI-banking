"""Public demo gateway; services and their databases stay on their original ports."""
import re
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, RedirectResponse, StreamingResponse

PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN', '').rstrip('/')
SITES = {'securities': 8001, 'food': 8002, 'travel': 8004, 'admin': 8003}
HOP = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'upgrade',
       'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'content-length',
       'accept-encoding', 'forwarded', 'x-forwarded-host', 'x-forwarded-proto'}


def rewrite(data, prefix):
    # Only known root-relative asset/API URLs; leave JS regexes and relative imports intact.
    return re.sub(rb'([\x22\x27\x60(])/(assets/|api/)',
                  lambda m: m[1] + prefix.encode() + b'/' + m[2], data)


def create_app(transport=None):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                timeout=httpx.Timeout(connect=5, read=None, write=30, pool=10)) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def proxy(request: Request, path: str):
        first, _, tail = path.partition('/')
        site = first if first in SITES else None
        prefix = '/' + site if site else ''
        if site and '/' not in path:
            return RedirectResponse(prefix + '/', status_code=307)
        upstream_path = '/' + tail if site else '/' + path
        if site and not (upstream_path == '/' or upstream_path == '/api/health'
                or upstream_path.startswith('/assets/')
                or upstream_path.startswith('/api/' + site + '/')
                or (site == 'securities' and upstream_path.startswith('/api/stocks/'))):
            return Response(status_code=404)
        origin = request.headers.get('origin')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and origin and origin not in {
                PUBLIC_ORIGIN, 'http://127.0.0.1:8010', 'http://localhost:8010'}:
            return Response('不接受来自其他站点的请求。', status_code=403)
        port = SITES[site] if site else 8000
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers['accept-encoding'] = 'identity'
        headers['x-forwarded-proto'] = 'https'
        if site:
            headers['x-forwarded-proto'] = 'http'
            # Upstream's existing CSRF policy is localhost-only. Validate above before translating.
            if origin:
                headers['origin'] = f'http://127.0.0.1:{port}'
            cookies = [(k, v) for k, v in request.cookies.items()
                       if (k == 'youshu_admin' if site == 'admin' else k.startswith(site + '_'))]
            headers.pop('cookie', None)
            if cookies:
                headers['cookie'] = '; '.join(f'{k}={v}' for k, v in cookies)
        url = httpx.URL(f'http://127.0.0.1:{port}{upstream_path}').copy_with(query=request.url.query.encode())
        client = app.state.client
        try:
            upstream = await client.send(client.build_request(request.method, url,
                headers=headers, content=await request.body()), stream=True)
        except httpx.RequestError:
            return Response('演示站点暂时无法连接，请稍后再试。', status_code=502)
        response_headers = []
        content_type = upstream.headers.get('content-type', '')
        transform = bool(site and any(t in content_type for t in ['text/html', 'javascript', 'text/css']))
        for key, value in upstream.headers.multi_items():
            if key.lower() in HOP or (transform and key.lower() in {'etag', 'content-encoding'}):
                continue
            if site and key.lower() == 'set-cookie':
                value = re.sub(r'(?i)(;\s*path=)/(?=;|$)', r'\g<1>' + prefix + '/', value)
                if 'secure' not in value.lower().split('; '):
                    value += '; Secure'
            if site and key.lower() == 'location' and value.startswith('/') and not value.startswith('//'):
                value = prefix + value
            response_headers.append((key.encode('latin1'), value.encode('latin1')))
        if transform:
            data = rewrite(await upstream.aread(), prefix)
            await upstream.aclose()
            response = Response(data, status_code=upstream.status_code)
            response_headers.append((b'cache-control', b'no-cache'))
        elif upstream.is_stream_consumed:
            response = Response(upstream.content, status_code=upstream.status_code)
            await upstream.aclose()
        else:
            response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                         background=BackgroundTask(upstream.aclose))
        response.raw_headers = response_headers
        return response

    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8010, log_level='warning', proxy_headers=True)
