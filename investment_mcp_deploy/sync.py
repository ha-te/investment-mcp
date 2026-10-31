"""Atomic endpoint/day imports; repeat interrupted units, retain completed units."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
import database as db
from jquants import Client, APIError, BASE, normalized_code

ENDPOINTS=('/equities/master','/equities/bars/daily','/fins/summary')


def canonical(row):
    raw=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return raw,hashlib.sha256(raw.encode()).hexdigest()


def identity(c,code):
    t=db.ticker(normalized_code(code))
    ids=c.execute("SELECT id FROM instruments WHERE ticker=? AND country='JP' ORDER BY CASE WHEN exchange='TSE' THEN 0 ELSE 1 END,id",(t,)).fetchall()
    if ids: return ids[0][0]
    raise ValueError('Unmapped issue code: '+t)


def ingest(endpoint,day,rows,path):
    if endpoint not in ENDPOINTS: raise ValueError('Unapproved endpoint')
    db.day(day)
    now=datetime.now(timezone.utc).isoformat()
    with db.transaction(path) as c:
        url=BASE+endpoint
        c.execute('INSERT OR IGNORE INTO sources(publisher,url,source_type,license_notes,retrieved_at) VALUES(?,?,?,?,?)',('J-Quants',url,'API','J-Quants subscription terms apply',now))
        sid=c.execute('SELECT id FROM sources WHERE url=?',(url,)).fetchone()[0]
        saved=0
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Invalid API record')
            raw,sha=canonical(row)
            if endpoint=='/equities/master':
                effective=db.day(row['Date'])
                if effective > day:raise ValueError('Future master date')
                t=db.ticker(normalized_code(row['Code']))
                name=row['CoName']
                if not isinstance(name,str) or not name.strip():raise ValueError('Missing company name')
                # Preserve all legacy instrument ids and columns.
                exists=c.execute("SELECT id FROM instruments WHERE ticker=? AND country='JP' AND exchange='TSE'",(t,)).fetchone()
                if not exists:
                    c.execute("INSERT INTO instruments(ticker,exchange,country) VALUES(?,'TSE','JP')",(t,))
                iid=identity(c,row['Code'])
                saved+=c.execute('INSERT OR IGNORE INTO jq_instrument_snapshots VALUES(?,?,?,?,?,?,?,?,?)',(iid,effective,name,row.get('S33Nm'),row.get('MktNm'),raw,sha,now,sid)).rowcount
            elif endpoint=='/equities/bars/daily':
                if db.day(row['Date'])!=day:raise ValueError('Mismatched price date')
                iid=identity(c,row['Code'])
                vals=[row.get(k) for k in ('O','H','L','C','Vo')]
                if all(v is None for v in vals):continue
                if any(v is None for v in vals):raise ValueError('Partially null OHLCV')
                o,h,l,close,vol=map(float,vals)
                adj=float(row['AdjC']) if row.get('AdjC') is not None else None
                if not (all(math.isfinite(v) for v in (o,h,l,close,vol)) and 0<l<=min(o,close)<=max(o,close)<=h and vol>=0 and (adj is None or math.isfinite(adj) and adj>0)):
                    raise ValueError('Invalid OHLCV')
                added=c.execute('INSERT OR IGNORE INTO jq_price_revisions(instrument_id,traded_on,raw_json,sha256,fetched_at,source_id) VALUES(?,?,?,?,?,?)',(iid,day,raw,sha,now,sid)).rowcount
                if added:
                    # Do not assert an invented publication timestamp. Fetch time is
                    # an upper bound on availability for this observed revision.
                    existing=c.execute('SELECT 1 FROM prices WHERE instrument_id=? AND traded_on=?',(iid,day)).fetchone()
                    if existing:
                        c.execute('UPDATE prices SET open=?,high=?,low=?,close=?,volume=?,adj_close=?,available_at=?,provider_source_id=? WHERE instrument_id=? AND traded_on=?',(o,h,l,close,vol,adj,now,sid,iid,day))
                    else:
                        c.execute('INSERT INTO prices(instrument_id,traded_on,open,high,low,close,volume,adj_close,available_at,provider_source_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(iid,day,o,h,l,close,vol,adj,now,sid))
                    c.execute('DELETE FROM investment_indicators WHERE instrument_id=?',(iid,))
                saved+=added
            else:
                if db.day(row['DiscDate'])!=day:raise ValueError('Mismatched disclosure date')
                iid=identity(c,row['Code'])
                for field in ('Sales','OP','OdP','NP','EPS','DEPS','TA','Eq','EqAR','BPS','CFO','CFI','CFF','CashEq'):
                    value=row.get(field)
                    if value not in (None,'') and (isinstance(value,bool) or not math.isfinite(float(value))):
                        raise ValueError('Invalid financial numeric value')
                number=str(row['DiscNo'])
                if not number or len(number)>80:raise ValueError('Invalid disclosure number')
                disc_time=row.get('DiscTime')
                if disc_time and not re.fullmatch(r'\d{2}:\d{2}(:\d{2})?',disc_time):raise ValueError('Invalid disclosure time')
                disclosed=datetime.fromisoformat(day+'T'+disc_time+'+09:00').isoformat() if disc_time else day
                period=db.day(row['CurPerEn']) if row.get('CurPerEn') else None
                saved+=c.execute('INSERT OR IGNORE INTO jq_financial_disclosures(instrument_id,disclosure_number,disclosed_at,period_end,document_type,raw_json,sha256,fetched_at,source_id) VALUES(?,?,?,?,?,?,?,?,?)',(iid,number,disclosed,period,row.get('DocType'),raw,sha,now,sid)).rowcount
        record(c,endpoint,day,'complete',len(rows),saved,None)
    return {'endpoint':endpoint,'day':day,'status':'complete','received':len(rows),'saved':saved}


def record(c,endpoint,day,status,received=0,saved=0,reason=None):
    c.execute('''INSERT INTO investment_sync_units(endpoint,day,status,rows_received,rows_saved,reason) VALUES(?,?,?,?,?,?) ON CONFLICT(endpoint,day) DO UPDATE SET status=excluded.status,rows_received=excluded.rows_received,rows_saved=excluded.rows_saved,reason=excluded.reason,finished_at=CURRENT_TIMESTAMP''',(endpoint,day,status,received,saved,reason))
    c.execute('INSERT INTO investment_import_runs(endpoint,day,status,rows_received,rows_saved,reason) VALUES(?,?,?,?,?,?)',(endpoint,day,status,received,saved,reason))


def sync_range(start,end,path,client=None,force=False):
    first,last=date.fromisoformat(db.day(start)),date.fromisoformat(db.day(end))
    if last<first or (last-first).days>30:raise ValueError('Choose at most 31 calendar days')
    db.migrate(path)
    client=client or Client()
    results=[]
    available=os.getenv('JQUANTS_AVAILABLE_THROUGH')
    earliest=os.getenv('JQUANTS_AVAILABLE_FROM')
    if available:db.day(available)
    if earliest:db.day(earliest)
    for offset in range((last-first).days+1):
        day=(first+timedelta(days=offset)).isoformat()
        # Query holidays too: a successful empty response is a durable checkpoint.
        for endpoint in ENDPOINTS:
            with db.connect(path) as c:
                prev=c.execute('SELECT status FROM investment_sync_units WHERE endpoint=? AND day=?',(endpoint,day)).fetchone()
            if prev and prev[0]=='complete' and not force:
                results.append({'endpoint':endpoint,'day':day,'status':'already_complete'});continue
            reason=None
            if available and day>available or earliest and day<earliest:
                reason='outside_configured_subscription_window'
            if reason:
                with db.transaction(path) as c:record(c,endpoint,day,'skipped',reason=reason)
                results.append({'endpoint':endpoint,'day':day,'status':'skipped','reason':reason});continue
            try:
                rows=client.get(endpoint,{'date':day})
                results.append(ingest(endpoint,day,rows,path))
                if endpoint=='/equities/bars/daily':db.persist_indicators(path,day)
            except APIError as exc:
                # 403 alone cannot distinguish plan restrictions from account denial.
                # Record the actual evidence; never mislabel auth failures as success.
                status='skipped' if exc.status==403 else 'failed'
                reason='access_denied_subscription_or_permission_unconfirmed' if exc.status==403 else 'upstream_http_'+str(exc.status)
                with db.transaction(path) as c:record(c,endpoint,day,status,reason=reason)
                results.append({'endpoint':endpoint,'day':day,'status':status,'reason':reason})
                if exc.status!=403:return {'status':'failed','units':results}
            except Exception:
                with db.transaction(path) as c:record(c,endpoint,day,'failed',reason='fetch_or_validation_failed')
                return {'status':'failed','units':results+[{'endpoint':endpoint,'day':day,'status':'failed','reason':'fetch_or_validation_failed'}]}
    return {'status':'partial' if any(r['status']=='skipped' for r in results) else 'complete','units':results}
