import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import database as db
from free_sync import execute,delta,local_schema,symbols,Remote
from test_production import MASTER,PRICE,FIN


class MemoryRemote:
    """Offline stand-in for the authenticated Worker API, using real SQLite."""
    def __init__(self,path):
        self.path=path;db.migrate(path);self.commits=0
        self.job={'id':'a'*32,'status':'queued','start_date':'2024-01-04','end_date':'2024-01-04','result_json':'{}'}
        with db.connect(path) as c:
            self.tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='investment_jobs' AND name NOT LIKE 'sqlite_%'")]
            self.schema={t:local_schema(c,t) for t in self.tables}
    def acquire(self):return {'lease':'b'*32,'tables':self.tables,'schema':self.schema}
    def close(self):pass
    def request(self,url,body=None,method=None):
        p=urlsplit(url)
        if p.path=='/internal/jobs':
            if body:
                if body['action']=='start':self.job['status']='running'
                elif body['action']=='finish':self.job['status']=body['status']
                return {'updated':True}
            return {'jobs':[self.job.copy()]}
        if p.path=='/internal/snapshot':
            q=parse_qs(p.query);t=q['table'][0];assert t in self.tables
            with db.connect(self.path) as c:
                return {'rows':[dict(r) for r in c.execute('SELECT rowid AS _sync_rowid,* FROM '+t+' WHERE rowid>? ORDER BY rowid LIMIT 250',(int(q['after'][0]),))]}
        if p.path=='/internal/commit':
            assert len(body['mutations'])<=500
            with db.transaction(self.path) as c:
                for m in body['mutations']:
                    t=m['table'];info=self.schema[t];pk=info['primary'];cols=info['columns']
                    if m['action']=='delete':c.execute('DELETE FROM '+t+' WHERE '+' AND '.join(k+'=?' for k in pk),m['values'])
                    else:
                        update=','.join(k+'=excluded.'+k for k in cols if k not in pk)
                        c.execute('INSERT INTO '+t+'('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+') ON CONFLICT('+','.join(pk)+') '+('DO UPDATE SET '+update if update else 'DO NOTHING'),m['values'])
            self.commits+=1
            return {'committed':len(body['mutations'])}
        raise AssertionError('Unexpected path')


class FakeClient:
    def __init__(self,bad=False):self.calls=[];self.bad=bad
    def get(self,endpoint,params):
        self.calls.append(endpoint)
        if endpoint=='/equities/master':return [MASTER,{**MASTER,'Code':'67580'}]
        if endpoint=='/equities/bars/daily':return [PRICE,{**PRICE,'Code':'67580'}]
        return [FIN,{**FIN,'Sales':'NaN'}] if self.bad else [FIN]


class Tests(unittest.TestCase):
    def test_existing_ingestion_to_remote_snapshot_and_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            remote=MemoryRemote(Path(d)/'remote.sqlite')
            self.assertEqual(execute(remote,client=FakeClient(),watchlist={'7203'}),'complete')
            self.assertEqual(db.statistics(remote.path)['counts']['instruments'],1)
            self.assertEqual(len(db.prices('7203',path=remote.path)),1)
            self.assertEqual(len(db.financials('7203',path=remote.path)),1)
            before=remote.commits;remote.job['status']='queued'
            client=FakeClient()
            self.assertEqual(execute(remote,client=client,watchlist={'7203'}),'complete')
            self.assertEqual(client.calls,[]);self.assertEqual(remote.commits,before)
    def test_failed_financial_batch_publishes_failure_and_preserves_valid_units(self):
        with tempfile.TemporaryDirectory() as d:
            remote=MemoryRemote(Path(d)/'remote.sqlite')
            self.assertEqual(execute(remote,client=FakeClient(bad=True),watchlist={'7203'}),'failed')
            self.assertEqual(len(db.prices('7203',path=remote.path)),1)
            self.assertEqual(db.financials('7203',path=remote.path),[])
            self.assertEqual(remote.job['status'],'failed')
            with db.connect(remote.path) as c:
                self.assertEqual(c.execute("SELECT status FROM investment_sync_units WHERE endpoint='/fins/summary'").fetchone()[0],'failed')
            remote.job['status']='queued';client=FakeClient()
            self.assertEqual(execute(remote,client=client,watchlist={'7203'}),'complete')
            self.assertEqual(client.calls,['/fins/summary'])
    def test_worker_migration_preserves_native_schema_contract(self):
        import sqlite3
        root=Path(__file__).resolve().parents[1]/'free_worker'
        metadata=json.loads((root/'src/schema.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'d1.sqlite'
            with sqlite3.connect(path) as c:c.executescript((root/'migrations/0001_investment.sql').read_text())
            db.migrate(path)
            with db.connect(path) as c:
                for table,info in metadata.items():self.assertEqual(local_schema(c,table),info)

    def test_schedule_uses_confirmed_free_availability_not_paid_recent_dates(self):
        from datetime import date
        from unittest.mock import patch
        from free_sync import scheduled_range
        with patch.dict('os.environ',{'JQUANTS_DATA_DELAY_DAYS':'10','JQUANTS_AVAILABLE_THROUGH':'2024-04-10','JQUANTS_AVAILABLE_FROM':'2024-04-08'},clear=True):
            self.assertEqual(scheduled_range(date(2024,5,1)),('2024-04-08','2024-04-10'))
        with patch.dict('os.environ',{'JQUANTS_DATA_DELAY_DAYS':'-1'},clear=True):
            with self.assertRaises(ValueError):scheduled_range(date(2024,5,1))

    def test_free_watchlist_and_transport_bounds(self):
        self.assertEqual(symbols('7203,6758,7203'),{'7203','6758'})
        for value in ['',"7203'",','.join(str(i) for i in range(1000,1051))]:
            with self.assertRaises(ValueError):symbols(value)
        with self.assertRaises(ValueError):Remote('http://example.test','x'*32)
        with self.assertRaises(ValueError):Remote('https://example.test','short')
    def test_oversized_delta_is_rejected_before_any_request(self):
        metadata={'t':{'columns':['id'],'primary':['id']}}
        with self.assertRaises(RuntimeError):delta({'t':{}},{'t':{(i,):(i,) for i in range(501)}},metadata)

if __name__=='__main__':unittest.main()
