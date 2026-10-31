"""J-Quants V2 connector. Key stays in the server's JQUANTS_API_KEY env var.
Official endpoint: https://api.jquants.com/v2/equities/bars/daily
"""
from __future__ import annotations
import json, os, sqlite3, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE='https://api.jquants.com/v2'

def key():
    value=os.environ.get('JQUANTS_API_KEY','').strip()
    if not value: raise RuntimeError('JQUANTS_API_KEY is not configured on the MCP host; do not paste it into chat')
    return value

class Client:
    def __init__(self, api_key=None, interval=12.2, opener=None, sleeper=time.sleep):
        self.api_key=api_key or key()
        self.interval=interval
        self.opener=opener or urllib.request.urlopen
        self.sleep=sleeper
        self.last_call=None
    def get(self, endpoint, params):
        if endpoint not in ('/equities/bars/daily','/equities/master','/fins/summary'):
            raise ValueError('Unapproved API endpoint')
        output=[]; cursor=None; visited=set()
        for _ in range(200):
            q={k:v for k,v in params.items() if v is not None}
            if cursor: q['pagination_key']=cursor
            url=BASE+endpoint+'?'+urllib.parse.urlencode(q)
            for attempt in range(5):
                if self.last_call is not None:
                    remaining=self.interval-(time.monotonic()-self.last_call)
                    if remaining>0:self.sleep(remaining)
                self.last_call=time.monotonic()
                req=urllib.request.Request(url,headers={'x-api-key':self.api_key,'Accept':'application/json','User-Agent':'InvestmentResearchMCP/0.2'})
                try:
                    with self.opener(req,timeout=35) as res:
                        raw=res.read(16_000_001)
                    if len(raw)>16_000_000:raise ValueError('Response exceeds 16MB')
                    obj=json.loads(raw)
                    if not isinstance(obj,dict) or not isinstance(obj.get('data'),list):raise ValueError('Unexpected J-Quants response shape')
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401,403):raise RuntimeError(f'J-Quants rejected authorization (HTTP {e.code}); check subscription and API key') from None
                    if e.code not in (429,500,502,503,504) or attempt==4:raise RuntimeError(f'J-Quants HTTP {e.code}') from None
                    retry=e.headers.get('Retry-After','') if e.headers else ''
                    try: delay=min(120,max(1,float(retry)))
                    except ValueError:delay=min(60,2**attempt*5)
                    self.sleep(delay)
                except (TimeoutError,urllib.error.URLError):
                    if attempt==4:raise
                    self.sleep(min(60,2**attempt*5))
            output.extend(obj['data'])
            cursor=obj.get('pagination_key')
            if not cursor:return output
            if cursor in visited:raise ValueError('Repeated pagination cursor')
            visited.add(cursor)
        raise ValueError('Pagination exceeds safety limit')

def normalized_code(code):
    s=str(code).strip().upper()
    if len(s)==5 and s[-1]=='0':return s[:4]
    if len(s)==4:return s
    raise ValueError('Unexpected J-Quants issue code: '+s)

def ensure_tables(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS jq_sync_days (
      traded_on TEXT PRIMARY KEY, rows_received INTEGER NOT NULL,
      rows_saved INTEGER NOT NULL, rows_no_trade INTEGER NOT NULL,
      synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE IF NOT EXISTS jq_unmapped_codes (
      code TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      occurrences INTEGER NOT NULL DEFAULT 1);
    ''')

def sync_day(day, db_path, client=None, dry_run=True):
    date.fromisoformat(day)
    client=client or Client()
    rows=client.get('/equities/bars/daily',{'date':day})
    parsed=[]; no_trade=0
    for r in rows:
        if not isinstance(r,dict) or r.get('Date')!=day:raise ValueError('Response contains mismatched date')
        ticker=normalized_code(r['Code'])
        vals=[r.get(k) for k in ('O','H','L','C','Vo')]
        if all(v is None for v in vals):
            no_trade+=1;continue
        if any(v is None for v in vals):raise ValueError('Partially null OHLCV')
        o,h,l,close,volume=map(float,vals)
        if not (0<l<=min(o,close)<=max(o,close)<=h and volume>=0):raise ValueError('Invalid OHLCV for '+ticker)
        adj=r.get('AdjC')
        parsed.append((ticker,day,o,h,l,close,volume,float(adj) if adj is not None else None))
    with sqlite3.connect(db_path) as c:
        c.execute('PRAGMA foreign_keys=ON')
        ensure_tables(c)
        ids={row[0]:row[1] for row in c.execute("SELECT ticker,id FROM instruments WHERE country='JP'")}
        unknown={r[0] for r in parsed if r[0] not in ids}
        result={'date':day,'received':len(rows),'valid_trades':len(parsed),'no_trade':no_trade,'unmapped':len(unknown),'saved':0,'dry_run':dry_run}
        if dry_run:return result
        # No silent loss of a trading day's data: unknown codes block completion.
        if unknown:
            for code in sorted(unknown):
                c.execute('''INSERT INTO jq_unmapped_codes(code,first_seen,last_seen) VALUES(?,?,?)
                ON CONFLICT(code) DO UPDATE SET last_seen=excluded.last_seen,occurrences=occurrences+1''',(code,day,day))
            result['sample_unmapped']=sorted(unknown)[:12]
            result['status']='incomplete_unmapped_codes'
            return result
        url=BASE+'/equities/bars/daily'
        c.execute('INSERT OR IGNORE INTO sources(publisher,url,source_type,license_notes,retrieved_at) VALUES (?,?,?,?,?)',
                  ('J-Quants',url,'API','Subject to J-Quants subscription and terms',datetime.now(timezone.utc).isoformat()))
        sid=c.execute('SELECT id FROM sources WHERE url=?',(url,)).fetchone()[0]
        before=c.total_changes
        c.executemany('''INSERT OR IGNORE INTO prices(instrument_id,traded_on,open,high,low,close,volume,adj_close,available_at,provider_source_id)
          VALUES (?,?,?,?,?,?,?,?,?,?)''',[(ids[t],d,o,h,l,cl,v,a,d+'T23:59:59+09:00',sid) for t,d,o,h,l,cl,v,a in parsed])
        saved=c.total_changes-before
        c.execute('INSERT INTO jq_sync_days(traded_on,rows_received,rows_saved,rows_no_trade) VALUES (?,?,?,?) ON CONFLICT(traded_on) DO UPDATE SET rows_received=excluded.rows_received,rows_saved=excluded.rows_saved,rows_no_trade=excluded.rows_no_trade,synced_at=CURRENT_TIMESTAMP',(day,len(rows),saved,no_trade))
        result['saved']=saved
        result['status']='complete'
        result['warning']='available_at is a conservative placeholder, NOT verified J-Quants publication time. Do not use for strict intraday backtests.'
        return result

def sync_range(start,end,db_path,client=None,dry_run=True,max_days=30):
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    if last<first or (last-first).days>max_days-1:raise ValueError(f'Choose a range of at most {max_days} calendar days')
    client=client or Client()
    results=[]
    for n in range((last-first).days+1):
        d=first+timedelta(days=n)
        if d.weekday()>=5:continue
        if not dry_run:
            with sqlite3.connect(db_path) as c:
                ensure_tables(c)
                if c.execute('SELECT 1 FROM jq_sync_days WHERE traded_on=?',(d.isoformat(),)).fetchone():continue
        result=sync_day(d.isoformat(),db_path,client,dry_run)
        results.append(result)
        if result.get('status')=='incomplete_unmapped_codes':break
    return {'days':results,'total_saved':sum(x['saved'] for x in results),'note':'Exchange holidays are requested but return no records; the Free plan excludes the latest period.'}
