"""Offline regression tests: temporary synthetic fixtures, never production data."""
import asyncio
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
import database as db
from sync import ingest, sync_range, ENDPOINTS
from jobs import Jobs
from jquants import Client, APIError
from remote_app import create_app
from security import JWTVerifier

MASTER={'Date':'2024-01-04','Code':'72030','CoName':'Synthetic test company','S33Nm':'Test','MktNm':'Test'}
PRICE={'Date':'2024-01-04','Code':'72030','O':100,'H':110,'L':90,'C':105,'Vo':1000,'AdjC':105}
FIN={'DiscDate':'2024-01-04','DiscTime':'15:00:00','Code':'72030','DiscNo':'test-1','CurPerEn':'2023-12-31','DocType':'test','Sales':100}

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'test.sqlite');db.migrate(self.path)
    def tearDown(self):self.tmp.cleanup()
    def count(self,t):
        with db.connect(self.path) as c:return c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
    def master(self):ingest(ENDPOINTS[0],MASTER['Date'],[MASTER],self.path)

class StorageTests(Fixture):
    def test_migration_repeat_and_existing_data(self):
        self.master();db.migrate(self.path);self.assertEqual(self.count('instruments'),1)
        with db.connect(self.path) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
    def test_incompatible_schema_rolls_back(self):
        p=str(Path(self.tmp.name)/'old.sqlite')
        with sqlite3.connect(p) as c:c.execute('CREATE TABLE instruments(id INTEGER PRIMARY KEY,legacy TEXT)')
        with self.assertRaises(RuntimeError):db.migrate(p)
        with sqlite3.connect(p) as c:
            self.assertEqual([r[1] for r in c.execute('PRAGMA table_info(instruments)')],['id','legacy'])
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='prices'").fetchone())
    def test_duplicate_and_revision_history(self):
        self.master()
        for row in [FIN,FIN,{**FIN,'Sales':110}]:ingest(ENDPOINTS[2],FIN['DiscDate'],[row],self.path)
        self.assertEqual(self.count('jq_financial_disclosures'),2)
        for row in [PRICE,PRICE,{**PRICE,'C':106,'AdjC':106}]:ingest(ENDPOINTS[1],PRICE['Date'],[row],self.path)
        self.assertEqual(self.count('prices'),1);self.assertEqual(self.count('jq_price_revisions'),2)
        self.assertEqual(db.prices('7203',path=self.path)[0]['close'],106)
        self.assertEqual(len(db.financials('7203',path=self.path)),2)
    def test_batch_atomic_rollback(self):
        self.master()
        with self.assertRaises(ValueError):ingest(ENDPOINTS[1],PRICE['Date'],[PRICE,{**PRICE,'Code':'99990'}],self.path)
        self.assertEqual(self.count('prices'),0);self.assertEqual(self.count('jq_price_revisions'),0)
    def test_nonfinite_and_invalid_dates_rejected(self):
        self.master()
        for row in [{**PRICE,'Vo':float('inf')},{**PRICE,'C':float('nan')},{**PRICE,'AdjC':-1},{**PRICE,'Date':'2024-01-05'}]:
            with self.assertRaises(ValueError):ingest(ENDPOINTS[1],PRICE['Date'],[row],self.path)
        self.assertEqual(self.count('prices'),0)
    def test_financial_rollback_and_online_backup(self):
        from maintenance import backup
        self.master()
        with self.assertRaises(ValueError):ingest(ENDPOINTS[2],FIN['DiscDate'],[FIN,{**FIN,'Sales':'NaN'}],self.path)
        self.assertEqual(self.count('jq_financial_disclosures'),0)
        dest=Path(self.tmp.name)/'backup.sqlite'
        backup(self.path,dest)
        with self.assertRaises(ValueError):backup(self.path,dest)
        with sqlite3.connect(dest) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM instruments').fetchone()[0],1)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_search_injection_and_bounds(self):
        self.master()
        self.assertEqual(len(db.search('7203',path=self.path)),1)
        self.assertEqual(db.search("' OR 1=1 --",path=self.path),[])
        self.assertEqual(db.search('%',path=self.path),[])
        with self.assertRaises(ValueError):db.prices("7203'",path=self.path)
        with self.assertRaises(ValueError):db.prices('7203',10000,self.path)
    def test_indicators_and_missing_history(self):
        self.master();self.assertEqual(db.indicators('7203',self.path)['status'],'insufficient_adjusted_prices')
        from datetime import date,timedelta
        for i in range(15):
            row={**PRICE,'Date':(date(2024,1,4)+timedelta(days=i)).isoformat()}
            ingest(ENDPOINTS[1],row['Date'],[row],self.path)
        result=db.indicators('7203',self.path)
        self.assertEqual(result['sma_5'],105);self.assertEqual(result['cutler_rsi_14'],50)
        self.assertIsNone(result['sma_25'])
    def test_resume_and_refresh(self):
        class Fake:
            def __init__(self):self.calls=[];self.fail=True
            def get(self,endpoint,params):
                self.calls.append(endpoint)
                if endpoint==ENDPOINTS[2] and self.fail:raise APIError(500)
                return {ENDPOINTS[0]:[MASTER],ENDPOINTS[1]:[PRICE],ENDPOINTS[2]:[FIN]}[endpoint]
        f=Fake();self.assertEqual(sync_range('2024-01-04','2024-01-04',self.path,f)['status'],'failed')
        f.fail=False;f.calls=[]
        self.assertEqual(sync_range('2024-01-04','2024-01-04',self.path,f)['status'],'complete')
        self.assertEqual(f.calls,[ENDPOINTS[2]])
        sync_range('2024-01-04','2024-01-04',self.path,f,force=True)
        self.assertEqual(self.count('prices'),1)
    def test_plan_window_and_auth_denial(self):
        class Denied:
            def get(self,*a):raise APIError(403)
        r=sync_range('2024-01-04','2024-01-04',self.path,Denied())
        self.assertEqual(r['status'],'partial');self.assertEqual(self.count('investment_import_runs'),3)
        with patch.dict(os.environ,{'JQUANTS_AVAILABLE_THROUGH':'2023-12-31'}):
            r=sync_range('2024-01-04','2024-01-04',self.path,Denied())
        self.assertEqual(r['units'][0]['reason'],'outside_configured_subscription_window')
        class Unauthorized:
            def get(self,*a):raise APIError(401)
        self.assertEqual(sync_range('2024-01-04','2024-01-04',self.path,Unauthorized())['status'],'failed')
    def test_job_lock_and_interrupted_recovery(self):
        jobs=Jobs(self.path);lock=jobs._lock()
        with self.assertRaises(ValueError):Jobs(self.path).submit('2024-01-04','2024-01-04','test')
        lock.close()
        with db.transaction(self.path) as c:c.execute("INSERT INTO investment_jobs(id,requested_by,start_date,end_date,status) VALUES('orphan','test','2024-01-04','2024-01-04','running')")
        class Empty:
            def get(self,*a):return []
        result=jobs.submit('2024-01-04','2024-01-04','test',background=False,client=Empty())
        self.assertEqual(jobs.status(result['job_id'])['status'],'complete')
        with db.connect(self.path) as c:self.assertEqual(c.execute("SELECT status FROM investment_jobs WHERE id='orphan'").fetchone()[0],'interrupted')
    def test_scheduled_weekday_and_manual_collision(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        jobs=Jobs(self.path)
        with patch.object(jobs,'submit') as submit:
            jobs.scheduled_tick(datetime(2026,10,31,19,tzinfo=ZoneInfo('Asia/Tokyo')))
            submit.assert_not_called()
            jobs.scheduled_tick(datetime(2026,10,30,19,tzinfo=ZoneInfo('Asia/Tokyo')))
            submit.assert_called_once()

class ClientTests(unittest.TestCase):
    def response(self,obj):
        raw=json.dumps(obj).encode()
        return SimpleResponse(raw)
    def test_header_auth_and_pagination(self):
        calls=[]
        def opener(req,timeout):
            calls.append(req)
            return self.response({'data':[{'n':len(calls)}],**({'pagination_key':'next'} if len(calls)==1 else {})})
        client=Client(api_key='test-secret',interval=0,opener=opener)
        self.assertEqual(len(client.get(ENDPOINTS[0],{'date':'2024-01-04'})),2)
        self.assertEqual(calls[0].get_header('X-api-key'),'test-secret')
        self.assertIn('pagination_key=next',calls[1].full_url)
    def test_retry_and_redacted_errors(self):
        n=0;delays=[]
        def opener(req,timeout):
            nonlocal n;n+=1
            if n==1:raise urllib.error.HTTPError(req.full_url,429,'SECRET',{'Retry-After':'2'},io.BytesIO(b'SECRET'))
            return self.response({'data':[]})
        self.assertEqual(Client(api_key='SECRET',interval=0,opener=opener,sleeper=delays.append).get(ENDPOINTS[0],{}),[])
        self.assertEqual(delays,[2])
        def denied(req,timeout):raise urllib.error.HTTPError(req.full_url,401,'SECRET',{},None)
        with self.assertRaises(APIError) as ctx:Client(api_key='SECRET',interval=0,opener=denied).get(ENDPOINTS[0],{})
        self.assertNotIn('SECRET',str(ctx.exception))
    def test_repeated_cursor_and_bad_shape(self):
        f=lambda req,timeout:self.response({'data':[],'pagination_key':'same'})
        with self.assertRaises(ValueError):Client(api_key='test',interval=0,opener=f).get(ENDPOINTS[0],{})
        f=lambda req,timeout:self.response({'data':{}})
        with self.assertRaises(ValueError):Client(api_key='test',interval=0,opener=f).get(ENDPOINTS[0],{})
    def test_redirect_refused(self):
        from jquants import NoRedirect
        with self.assertRaises(APIError):NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test')

class SimpleResponse:
    def __init__(self,raw):self.raw=raw
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):return self.raw[:n]

class SecurityTests(Fixture):
    @classmethod
    def setUpClass(cls):cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,{'INVESTMENT_DB':self.path,'OAUTH_ISSUER':'https://issuer.example/','MCP_RESOURCE_URL':'https://mcp.example/mcp','INVESTMENT_OWNER_SUB':'owner','INVESTMENT_SCHEDULE_ENABLED':'false'})
        self.env.start()
        key=self.key.public_key()
        self.verifier=JWTVerifier('https://issuer.example/','https://mcp.example/mcp',SimpleNamespace(get_signing_key_from_jwt=lambda token:SimpleNamespace(key=key)))
    def tearDown(self):self.env.stop();super().tearDown()
    def token(self,subject='reader',scope='investment:read',**extra):
        return jwt.encode({'iss':'https://issuer.example/','aud':'https://mcp.example/mcp','sub':subject,'iat':int(time.time()),'exp':int(time.time())+600,'scope':scope,**extra},self.key,algorithm='RS256')
    def test_jwt_claims_and_algorithm(self):
        self.assertIsNotNone(asyncio.run(self.verifier.verify_token(self.token())))
        for token in [self.token(aud='wrong'),self.token(iss='https://evil/'),self.token(exp=1),jwt.encode({'sub':'owner'},'x'*32,algorithm='HS256'),'SECRET']:
            self.assertIsNone(asyncio.run(self.verifier.verify_token(token)))
    def client(self):return TestClient(create_app(self.verifier,check_volume=False),base_url='https://mcp.example')
    def request(self,client,name,args,token=None):
        headers={'Accept':'application/json, text/event-stream'}
        if token:headers['Authorization']='Bearer '+token
        return client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},headers=headers)
    def test_unauthenticated_and_wrong_scope(self):
        with self.client() as c:
            self.assertEqual(c.get('/healthz').status_code,200)
            response=self.request(c,'database_status',{})
            self.assertEqual(response.status_code,401)
            self.assertIn('resource_metadata',response.headers['www-authenticate'])
            self.assertEqual(self.request(c,'database_status',{},self.token(scope='investment:update')).status_code,403)
            self.assertEqual(self.request(c,'database_status',{},'SECRET').status_code,401)
    def test_read_and_owner_scope_separation(self):
        with self.client() as c:
            response=self.request(c,'database_status',{},self.token())
            self.assertEqual(response.status_code,200,response.text)
            self.assertFalse(response.json()['result']['isError'])
            for token in [self.token(),self.token(scope='investment:read investment:update'),self.token('owner')]:
                response=self.request(c,'run_update',{'start_date':'2024-01-04','end_date':'2024-01-04'},token)
                self.assertTrue(response.json()['result']['isError'],response.text)
            with patch('jobs.Jobs.submit',return_value={'job_id':'test'}) as submit:
                response=self.request(c,'run_update',{'start_date':'2024-01-04','end_date':'2024-01-04'},self.token('owner','investment:read investment:update'))
                self.assertFalse(response.json()['result']['isError'],response.text);submit.assert_called_once()
    def test_metadata_and_no_local_file_tool(self):
        with self.client() as c:
            metadata=c.get('/.well-known/oauth-protected-resource/mcp')
            self.assertEqual(metadata.status_code,200,metadata.text)
            self.assertEqual(metadata.json()['resource'],'https://mcp.example/mcp')
            response=c.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/list'},headers={'Authorization':'Bearer '+self.token(),'Accept':'application/json, text/event-stream'})
            names={t['name'] for t in response.json()['result']['tools']}
            self.assertEqual(len(names),7);self.assertNotIn('import_prices_csv_file',names)
    def test_errors_and_request_limits(self):
        with self.client() as c:
            response=self.request(c,'lookup_price',{'ticker':'SECRET\' OR 1=1 --'},self.token())
            self.assertNotIn('SECRET',response.text)
            self.assertNotIn(self.path,response.text)
            self.assertEqual(c.post('/mcp',content=b'x'*65537).status_code,413)
            for _ in range(125):response=c.post('/mcp',json={})
            self.assertEqual(response.status_code,429)
    def test_proxy_flag_does_not_authorize(self):
        with patch.dict(os.environ,{'INVESTMENT_TRUSTED_AUTH_PROXY':'1'}):
            with self.client() as c:self.assertEqual(self.request(c,'database_status',{}).status_code,401)
        with self.assertRaises(ValueError):create_app(self.verifier,check_volume=True)

class StdioTests(Fixture):
    def test_real_stdio_process(self):
        import sys
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async def check():
            params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('server.py'))],env={**os.environ,'INVESTMENT_DB':self.path,'PYTHONDONTWRITEBYTECODE':'1'})
            with anyio.fail_after(15):
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        self.assertEqual(len((await session.list_tools()).tools),7)
                        result=await session.call_tool('database_status',{})
                        self.assertFalse(result.isError)
                        data=result.structuredContent or json.loads(result.content[0].text)
                        self.assertEqual(data['counts']['prices'],0)
        asyncio.run(check())

if __name__=='__main__':unittest.main()
