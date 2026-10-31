"""Investment MCP: public sources + local CSV ingestion, no secrets required.
Run: pip install -r requirements.txt && python server.py
"""
from __future__ import annotations
import csv, hashlib, io, json, os, sqlite3, time, urllib.parse, urllib.request
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DB = Path(os.getenv('INVESTMENT_DB', str(Path(__file__).with_name('investment_phase8.sqlite'))))
AGENT = os.getenv('INVESTMENT_USER_AGENT', 'InvestmentResearch/0.1 research-contact@example.com')
MAX_BYTES = 12_000_000


def connection():
    con=sqlite3.connect(DB)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=10000')
    return con


def init():
    from database import migrate
    migrate(DB)
    with connection() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS mcp_import_runs (
          id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, sha256 TEXT NOT NULL,
          rows_seen INTEGER NOT NULL, rows_saved INTEGER NOT NULL,
          status TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS mcp_external_facts (
          id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, metric TEXT NOT NULL,
          period_end TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
          source_url TEXT NOT NULL, source_sha256 TEXT NOT NULL,
          fetched_at TEXT NOT NULL, available_at TEXT,
          UNIQUE(symbol,metric,period_end,unit,source_url));
        CREATE INDEX IF NOT EXISTS ix_mcp_external_facts_symbol ON mcp_external_facts(symbol,metric,period_end);
        ''')


def request_bytes(url: str, allowed_hosts: set[str], timeout: int=25)->bytes:
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts:
        raise ValueError('HTTPS and an allowlisted source domain are required')
    req=urllib.request.Request(url,headers={'User-Agent':AGENT,'Accept':'application/json,text/csv,*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        if urllib.parse.urlsplit(resp.geturl()).hostname not in allowed_hosts:
            raise ValueError('Redirect outside allowlist blocked')
        data=resp.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise ValueError('Download exceeds 12 MB')
    return data


def instrument_id(c, ticker: str)->int|None:
    r=c.execute('SELECT id FROM instruments WHERE ticker=? AND country=? ORDER BY CASE WHEN exchange="TSE" THEN 0 ELSE 1 END LIMIT 1',(ticker,'JP')).fetchone()
    if not r:
        r=c.execute('SELECT id FROM instruments WHERE ticker=? LIMIT 1',(ticker,)).fetchone()
    return r[0] if r else None


def ingest_price_csv(csv_text: str, source_url='manual:csv', dry_run=False)->dict:
    """Header: ticker,date,open,high,low,close,volume[,adj_close]. Never invent availability dates."""
    init()
    raw=csv_text.encode('utf-8-sig'); sha=hashlib.sha256(raw).hexdigest()
    rdr=csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
    required={'ticker','date','open','high','low','close','volume'}
    if not rdr.fieldnames or not required.issubset(set(rdr.fieldnames)):
        raise ValueError('CSV must include '+','.join(sorted(required)))
    prepared=[]; missing=set()
    with connection() as c:
        for line,row in enumerate(rdr,start=2):
            ticker=(row['ticker'] or '').strip().upper()
            dt=date.fromisoformat(row['date'].strip()).isoformat()
            o,h,l,cl=[float(row[x]) for x in ('open','high','low','close')]
            vol=float(row['volume'])
            adj=float(row['adj_close']) if row.get('adj_close') else None
            if not (all(math.isfinite(v) for v in (o,h,l,cl,vol)) and ticker and l>0 and l<=min(o,cl)<=max(o,cl)<=h and vol>=0 and (adj is None or adj>0)):
                raise ValueError(f'Invalid OHLCV at line {line}')
            iid=instrument_id(c,ticker)
            if iid is None: missing.add(ticker); continue
            prepared.append((iid,dt,o,h,l,cl,vol,adj))
        if dry_run:
            return {'rows_valid':len(prepared),'unknown_tickers':sorted(missing),'saved':0,'dry_run':True}
        c.execute('INSERT OR IGNORE INTO sources(publisher,url,source_type,license_notes,retrieved_at) VALUES (?,?,?,?,?)',
                  ('User-supplied CSV',source_url,'CSV','Verify permission to use this dataset',datetime.now(timezone.utc).isoformat()))
        sid=c.execute('SELECT id FROM sources WHERE url=?',(source_url,)).fetchone()[0]
        before=c.total_changes
        # Preserve previous observations and their provenance. Explicit updates require separate audited logic.
        c.executemany('''INSERT OR IGNORE INTO prices(instrument_id,traded_on,open,high,low,close,volume,adj_close,available_at,provider_source_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      [(i,d,o,h,l,cl,v,adj,d+'T15:30:00+09:00',sid) for i,d,o,h,l,cl,v,adj in prepared])
        saved=c.total_changes-before
        c.execute('INSERT INTO mcp_import_runs(source_url,sha256,rows_seen,rows_saved,status) VALUES(?,?,?,?,?)',
                  (source_url,sha,len(prepared)+len(missing),saved,'completed'))
        return {'rows_valid':len(prepared),'unknown_tickers':sorted(missing),'saved':saved,'dry_run':False,
                'note':'available_at is a conservative session-close proxy, NOT actual publication time; exclude from strict point-in-time backtests until verified.'}


def import_local_csv(path: str, dry_run: bool=False)->dict:
    p=Path(path).expanduser().resolve()
    if not p.is_file() or p.stat().st_size>MAX_BYTES: raise ValueError('Missing file or over size limit')
    return ingest_price_csv(p.read_text(encoding='utf-8-sig'),source_url='file:'+p.name,dry_run=dry_run)


def fetch_sec_companyfacts(cik: str, ticker: str, dry_run: bool=False)->dict:
    """SEC companyfacts is public. Set a truthful contact email in INVESTMENT_USER_AGENT before use."""
    if AGENT.endswith('example.com'): raise ValueError('Set INVESTMENT_USER_AGENT to a truthful name and contact email for SEC requests')
    digits=''.join(ch for ch in cik if ch.isdigit())
    if not digits or len(digits)>10: raise ValueError('Invalid CIK')
    url=f'https://data.sec.gov/api/xbrl/companyfacts/CIK{int(digits):010d}.json'
    data=request_bytes(url,{'data.sec.gov'})
    obj=json.loads(data); sha=hashlib.sha256(data).hexdigest()
    facts=obj.get('facts',{}).get('us-gaap',{})
    wanted={'Revenues':'revenue','RevenueFromContractWithCustomerExcludingAssessedTax':'revenue',
            'NetIncomeLoss':'net_income','Assets':'total_assets','StockholdersEquity':'equity',
            'EarningsPerShareDiluted':'diluted_eps'}
    entries=[]
    for key,metric in wanted.items():
        for unit,rows in facts.get(key,{}).get('units',{}).items():
            for x in rows:
                if not all(k in x for k in ('end','val','filed')): continue
                if x.get('form') not in ('10-K','10-Q','10-K/A','10-Q/A'): continue
                # Preserve SEC accession/version and filing date in availability (not period-end).
                entries.append((ticker.upper(),metric,x['end'],float(x['val']),unit,url,sha,
                                datetime.now(timezone.utc).isoformat(),x['filed']))
    if dry_run:return {'records_parsed':len(entries),'saved':0,'source':url,'dry_run':True}
    init()
    with connection() as c:
        before=c.total_changes
        c.executemany('''INSERT OR IGNORE INTO mcp_external_facts
        (symbol,metric,period_end,value,unit,source_url,source_sha256,fetched_at,available_at)
        VALUES (?,?,?,?,?,?,?,?,?)''',entries)
        saved=c.total_changes-before
    return {'records_parsed':len(entries),'saved':saved,'source':url,'dry_run':False,
            'warning':'SEC filing dates are date-only; use accession-level timestamps for intraday backtests. SEC amendments and alternative revenue tags require further normalization.'}


def status()->dict:
    init()
    with connection() as c:
        counts={t:c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('instruments','prices','financial_facts','mcp_external_facts','mcp_import_runs')}
        return {'database':str(DB),'counts':counts,'credentials_required_for_public_sec':False,
                'jquants_key_configured':bool(os.getenv('JQUANTS_API_KEY'))}


def register_mcp():
    from app import create_mcp
    return create_mcp(path=DB)

if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='status': print(json.dumps(status(),ensure_ascii=False,indent=2))
    elif len(sys.argv)>2 and sys.argv[1]=='import': print(json.dumps(import_local_csv(sys.argv[2],dry_run='--commit' not in sys.argv),ensure_ascii=False,indent=2))
    else:register_mcp().run(transport='stdio')
