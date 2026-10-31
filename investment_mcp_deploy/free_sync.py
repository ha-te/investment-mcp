"""Free runner: validate with existing SQLite/Python, publish atomic D1 deltas.

Only explicitly configured watchlist data is retained. Temporary snapshots never
become GitHub artifacts. A failed upload never advances the remote checkpoint.
"""
from __future__ import annotations
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date,datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import database as db
from jquants import Client,NoRedirect,normalized_code
from sync import sync_range


class Remote:
    def __init__(self,url,key,opener=None):
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
            raise ValueError('Configure the canonical HTTPS Worker origin')
        if len(key)<32 or len(key)>128:raise ValueError('Configure a random sync key of 32 to 128 characters')
        self.url=url.rstrip('/');self.key=key;self.lease=None
        self.opener=opener or urllib.request.build_opener(NoRedirect()).open
    def request(self,path,body=None,method=None):
        headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'}
        if self.lease:headers['X-Sync-Lease']=self.lease
        data=json.dumps(body,allow_nan=False).encode() if body is not None else None
        req=urllib.request.Request(self.url+path,data=data,headers=headers,method=method)
        for attempt in range(3):
            try:
                with self.opener(req,timeout=30) as r:raw=r.read(8_000_001)
                if len(raw)>8_000_000:raise RuntimeError('Snapshot page exceeds memory limit')
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                if exc.code==429 and attempt<2:
                    time.sleep(60)
                    continue
                raise RuntimeError('Free sync service unavailable; inspect configuration and free quotas') from None
            except Exception:raise RuntimeError('Free sync service unavailable; inspect configuration and free quotas') from None
    def acquire(self):
        result=self.request('/internal/lease',{},'POST');self.lease=result['lease'];return result
    def close(self):
        if self.lease:self.request('/internal/lease',method='DELETE');self.lease=None


def local_schema(c,table):
    columns=list(c.execute('PRAGMA table_info('+table+')'))
    return {'columns':[r['name'] for r in columns],'primary':[r['name'] for r in sorted(columns,key=lambda x:x['pk']) if r['pk']]}


def snapshot(c,metadata):
    result={}
    for table,info in metadata.items():
        cols=info['columns'];pk=info['primary'];positions=[cols.index(k) for k in pk]
        records={}
        for row in c.execute('SELECT '+','.join(cols)+' FROM '+table):
            values=tuple(row);records[tuple(values[i] for i in positions)]=values
        result[table]=records
    return result


def delta(previous,current,metadata):
    mutations=[]
    # Deletes currently concern derived indicators only. The remote D1 batch
    # enforces FKs and rejects any unsafe parent removal atomically.
    for table in reversed(metadata):
        for key in previous[table].keys()-current[table].keys():
            mutations.append({'table':table,'action':'delete','values':list(key)})
    for table in metadata:
        for key,values in current[table].items():
            if previous[table].get(key)!=values:
                mutations.append({'table':table,'action':'upsert','values':list(values)})
    if len(mutations)>500:raise RuntimeError('Free atomic batch exceeds 500 rows; reduce the configured watchlist')
    return mutations


class Publisher:
    def __init__(self,remote,path,lease):
        self.remote=remote;self.path=path
        with db.connect(path) as c:
            known={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.metadata={}
            for table in lease['tables']:
                if table not in known or table=='investment_jobs':raise RuntimeError('Unexpected remote schema')
                info=local_schema(c,table)
                if info!=lease['schema'][table]:raise RuntimeError('Remote schema version is incompatible')
                self.metadata[table]=info
        # Reconstruct a disposable SQLite copy, keeping the remote primary keys.
        with db.connect(path) as c:
            c.execute('PRAGMA foreign_keys=OFF')
            for table,info in self.metadata.items():
                after=0
                while True:
                    page=remote.request('/internal/snapshot?'+urllib.parse.urlencode({'table':table,'after':after}))['rows']
                    if not page:break
                    for row in page:
                        values=[row[col] for col in info['columns']]
                        c.execute('INSERT OR REPLACE INTO '+table+'('+','.join(info['columns'])+') VALUES('+','.join('?' for _ in values)+')',values)
                    after=page[-1]['_sync_rowid']
            if c.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('Remote snapshot has invalid references')
        with db.connect(path) as c:self.previous=snapshot(c,self.metadata)
    def publish(self):
        with db.connect(self.path) as c:current=snapshot(c,self.metadata)
        changes=delta(self.previous,current,self.metadata)
        if changes:self.remote.request('/internal/commit',{'mutations':changes},'POST')
        self.previous=current


class WatchlistClient:
    def __init__(self,symbols,client):self.symbols=set(symbols);self.client=client
    def get(self,endpoint,params):
        data=self.client.get(endpoint,params)
        return [row for row in data if db.ticker(normalized_code(row['Code'])) in self.symbols]


def symbols(value):
    result={db.ticker(code.strip()) for code in value.split(',') if code.strip()}
    if not 1<=len(result)<=50:raise ValueError('Explicitly configure 1 to 50 watchlist codes')
    return result


def scheduled_range(today=None):
    today=today or datetime.now(ZoneInfo('Asia/Tokyo')).date()
    delay=int(os.getenv('JQUANTS_DATA_DELAY_DAYS','0'))
    if not 0<=delay<=3660:raise ValueError('Invalid subscription data delay')
    end=today-timedelta(days=delay)
    available=os.getenv('JQUANTS_AVAILABLE_THROUGH')
    earliest=os.getenv('JQUANTS_AVAILABLE_FROM')
    if available:end=min(end,date.fromisoformat(db.day(available)))
    start=end-timedelta(days=6)
    if earliest:start=max(start,date.fromisoformat(db.day(earliest)))
    if start>end:raise ValueError('No dates inside the configured free subscription window')
    return start.isoformat(),end.isoformat()


def execute(remote,job_id=None,client=None,watchlist=None):
    lease=remote.acquire();job=None
    try:
        queued=remote.request('/internal/jobs'+('?id='+job_id if job_id else ''))['jobs']
        if not queued and job_id:raise ValueError('Unknown requested job')
        if not queued:
            start,end=scheduled_range()
            job_id=remote.request('/internal/jobs',{'action':'schedule','start_date':start,'end_date':end},'POST')['job_id']
            queued=remote.request('/internal/jobs?id='+job_id)['jobs']
        job=queued[0]
        if job['status'] in ('complete','partial'):return job['status']
        remote.request('/internal/jobs',{'action':'start','job_id':job['id']},'POST')
        if watchlist is None:watchlist=symbols(os.getenv('INVESTMENT_SYMBOLS',''))
        if client is None:client=Client()
        first,last=db.day(job['start_date']),db.day(job['end_date'])
        if (datetime.fromisoformat(last)-datetime.fromisoformat(first)).days not in range(7):raise ValueError('Free update range exceeds seven days')
        options=json.loads(job['result_json'] or '{}')
        with tempfile.TemporaryDirectory(prefix='investment-free-') as d:
            path=Path(d)/'snapshot.sqlite';db.migrate(path)
            publisher=Publisher(remote,path,lease)
            result=sync_range(first,last,path,WatchlistClient(watchlist,client),force=bool(options.get('refresh')),on_unit=publisher.publish)
            publisher.publish()  # persist skipped/failed unit reasons as well
        remote.request('/internal/jobs',{'action':'finish','job_id':job['id'],'status':result['status']},'POST')
        return result['status']
    except Exception:
        if job:
            try:remote.request('/internal/jobs',{'action':'finish','job_id':job['id'],'status':'failed','reason':'update_failed'},'POST')
            except Exception:pass
        raise RuntimeError('Free update failed; verify secrets, subscription availability and free quotas') from None
    finally:
        try:remote.close()
        except Exception:pass


def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--job-id');args=p.parse_args()
    # Values are never printed. These are user-provisioned secrets on Free plans.
    try:
        if args.job_id and (len(args.job_id)!=32 or any(x not in '0123456789abcdef' for x in args.job_id)):raise ValueError('Invalid job id')
        result=execute(Remote(os.environ['FREE_MCP_ORIGIN'],os.environ['SYNC_KEY']),args.job_id)
        print('Free update status: '+result)
        return 0 if result=='complete' else 1
    except Exception:
        print('Free update failed; inspect configuration, subscription availability and free quotas')
        return 1
if __name__=='__main__':raise SystemExit(main())
