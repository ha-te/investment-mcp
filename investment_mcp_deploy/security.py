"""OAuth resource-server validation: no trusted-header or environment-flag auth."""
from __future__ import annotations
import asyncio
import os
import time
from collections import deque
from urllib.parse import urlsplit
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.auth_context import get_access_token


def https_url(value):
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Expected a canonical HTTPS URL')
    return value


class JWTVerifier:
    def __init__(self,issuer,resource,keys=None):
        self.issuer=https_url(issuer)
        self.resource=https_url(resource)
        # The URL is operator supplied and fixed, never taken from JWT jku/x5u.
        self.keys=keys or jwt.PyJWKClient(issuer.rstrip('/')+'/.well-known/jwks.json',timeout=10,cache_jwk_set=True,lifespan=300)

    async def verify_token(self,token):
        if len(token)>16384:return None
        try:
            key=await asyncio.to_thread(self.keys.get_signing_key_from_jwt,token)
            claims=jwt.decode(token,key.key,algorithms=['RS256'],audience=self.resource,issuer=self.issuer,options={'require':['exp','iat','iss','aud','sub']},leeway=10)
            if not isinstance(claims['sub'],str) or not claims['sub']:return None
            scope=claims.get('scope','')
            if not isinstance(scope,str):return None
            return AccessToken(token=token,client_id=claims.get('azp',claims['sub']),subject=claims['sub'],scopes=scope.split(),expires_at=int(claims['exp']),resource=self.resource)
        except Exception:
            # Do not log tokens, claims, JWKS errors or remote error bodies.
            return None


def require_owner(owner,local=False):
    if local:return 'local-owner'
    access=get_access_token()
    if access is None or access.subject!=owner or 'investment:update' not in access.scopes:
        raise PermissionError('Owner authorization required')
    return access.subject


class Limits:
    """Global request ceiling, body bound, and opaque errors; no forwarded trust.

    A single personal instance intentionally uses a global rate limit rather than
    trusting forgeable X-Forwarded-For headers. Health checks are exempt.
    """
    def __init__(self,app,per_minute=120,max_body=65536):
        self.app=app;self.per_minute=per_minute;self.max_body=max_body;self.times=deque()

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        from starlette.responses import JSONResponse
        if scope['path']=='/healthz':return await self.app(scope,receive,send)
        now=time.monotonic()
        while self.times and self.times[0]<now-60:self.times.popleft()
        if len(self.times)>=self.per_minute:
            return await JSONResponse({'error':'rate_limited'},429,headers={'Retry-After':'60'})(scope,receive,send)
        self.times.append(now)
        chunks=[];size=0
        while True:
            try:message=await asyncio.wait_for(receive(),timeout=15)
            except asyncio.TimeoutError:
                return await JSONResponse({'error':'request_timeout'},408)(scope,receive,send)
            if message['type']=='http.disconnect':return
            size+=len(message.get('body',b''))
            if size>self.max_body:
                return await JSONResponse({'error':'request_too_large'},413)(scope,receive,send)
            chunks.append(message)
            if not message.get('more_body',False):break
        async def replay():
            if chunks:return chunks.pop(0)
            return await receive()
        started=False
        async def safe_send(message):
            nonlocal started
            if message['type']=='http.response.start':started=True
            await send(message)
        try:await self.app(scope,replay,safe_send)
        except Exception:
            if not started:await JSONResponse({'error':'internal_error'},500)(scope,receive,send)
