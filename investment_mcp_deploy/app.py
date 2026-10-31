"""Shared read tools and explicitly authorized owner job controls."""
from contextlib import asynccontextmanager
import os
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
import database as db
from jobs import Jobs
from security import JWTVerifier, require_owner, https_url


def create_mcp(remote=False,verifier=None,path=None):
    path=path or db.db_path()
    db.migrate(path)
    jobs=Jobs(path)
    owner=os.getenv('INVESTMENT_OWNER_SUB','')
    options={}
    if remote:
        issuer=https_url(os.environ['OAUTH_ISSUER'])
        resource=https_url(os.environ['MCP_RESOURCE_URL'])
        from urllib.parse import urlsplit
        if not owner:raise ValueError('INVESTMENT_OWNER_SUB is required')
        host=urlsplit(resource).netloc
        options=dict(token_verifier=verifier or JWTVerifier(issuer,resource),auth=AuthSettings(issuer_url=AnyHttpUrl(issuer),resource_server_url=AnyHttpUrl(resource),required_scopes=['investment:read'],validate_token_resource=True),transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=[host,'127.0.0.1:*','localhost:*'],allowed_origins=['https://'+host]))
    @asynccontextmanager
    async def lifespan(server):
        if remote and os.getenv('INVESTMENT_SCHEDULE_ENABLED','false')=='true':jobs.start_scheduler()
        try:yield {}
        finally:jobs.close()
    mcp=FastMCP('investment-data',host='0.0.0.0' if remote else '127.0.0.1',port=int(os.getenv('PORT','8000')),stateless_http=True,json_response=True,log_level='WARNING',max_request_body_size=65536,lifespan=lifespan,**options)
    # Wrappers deliberately omit exception details from tool responses. SQLite,
    # upstream response bodies and paths must not be exposed through errors.
    def safe(fn,*args):
        try:return fn(*args)
        except PermissionError:raise ValueError('Owner authorization required') from None
        except Exception:raise ValueError('Operation failed; check input and server configuration') from None
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def search_instruments(query:str,limit:int=20)->list[dict]:
        """Search stored Japanese codes and company names; bounded literal search."""
        return safe(db.search,query,limit,path)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def lookup_price(ticker:str,limit:int=30)->list[dict]:
        """Stored OHLCV, newest first. available_at is observation time for J-Quants imports."""
        return safe(db.prices,ticker,limit,path)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def financial_information(ticker:str,limit:int=20)->list[dict]:
        """Financial disclosures and correction history; preserve raw v2 field names."""
        return safe(db.financials,ticker,limit,path)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def technical_indicators(ticker:str)->dict:
        """Adjusted-close SMA 5/25/75 and Cutler RSI 14. Missing history returns nulls."""
        return safe(db.indicators,ticker,path)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def database_status()->dict:
        """Stored data counts, latest price date and recent endpoint/day update status."""
        return safe(db.statistics,path)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=True))
    def run_update(start_date:str,end_date:str,refresh_completed:bool=False)->dict:
        """OWNER ONLY: queue at most 31 days. Refresh recent completed dates to ingest corrections."""
        subject=safe(require_owner,owner,not remote)
        return safe(jobs.submit,start_date,end_date,subject,refresh_completed)
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    def update_job_status(job_id:str)->dict:
        """OWNER ONLY: inspect a durable update job and safe per-endpoint outcome."""
        safe(require_owner,owner,not remote)
        return safe(jobs.status,job_id)
    return mcp
