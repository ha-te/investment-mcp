"""Additive SQLite migrations and bounded, parameterized analysis queries."""
from __future__ import annotations
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path


def db_path():
    return Path(os.getenv('INVESTMENT_DB', str(Path(__file__).with_name('investment_phase8.sqlite'))))


class Connection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()


def connect(path=None):
    c = sqlite3.connect(path or db_path(), timeout=15, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=15000')
    return c


@contextmanager
def transaction(path=None):
    c = connect(path)
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


CORE = {
'instruments': '''CREATE TABLE instruments(id INTEGER PRIMARY KEY,ticker TEXT NOT NULL,exchange TEXT NOT NULL,country TEXT NOT NULL,UNIQUE(ticker,exchange,country))''',
'sources': '''CREATE TABLE sources(id INTEGER PRIMARY KEY,publisher TEXT,url TEXT UNIQUE,source_type TEXT,license_notes TEXT,retrieved_at TEXT)''',
'prices': '''CREATE TABLE prices(instrument_id INTEGER NOT NULL REFERENCES instruments(id),traded_on TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume REAL,adj_close REAL,available_at TEXT,provider_source_id INTEGER REFERENCES sources(id),PRIMARY KEY(instrument_id,traded_on))''',
'financial_facts': '''CREATE TABLE financial_facts(id INTEGER PRIMARY KEY)''',
}
REQUIRED = {
'instruments': {'id','ticker','exchange','country'},
'sources': {'id','publisher','url','source_type','license_notes','retrieved_at'},
'prices': {'instrument_id','traded_on','open','high','low','close','volume','adj_close','available_at','provider_source_id'},
'financial_facts': {'id'},
}
DDL = [
'''CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_prices ON prices(instrument_id,traded_on)''',
'''CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_sources ON sources(url)''',
'''CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_instruments ON instruments(ticker,exchange,country)''',
'''CREATE TABLE IF NOT EXISTS investment_schema_versions(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS jq_instrument_snapshots(instrument_id INTEGER NOT NULL REFERENCES instruments(id),effective_on TEXT NOT NULL,name TEXT NOT NULL,sector TEXT,market TEXT,raw_json TEXT NOT NULL,sha256 TEXT NOT NULL,fetched_at TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES sources(id),PRIMARY KEY(instrument_id,effective_on,sha256))''',
'''CREATE TABLE IF NOT EXISTS jq_financial_disclosures(id INTEGER PRIMARY KEY,instrument_id INTEGER NOT NULL REFERENCES instruments(id),disclosure_number TEXT NOT NULL,disclosed_at TEXT NOT NULL,period_end TEXT,document_type TEXT,raw_json TEXT NOT NULL,sha256 TEXT NOT NULL,fetched_at TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES sources(id),UNIQUE(instrument_id,disclosure_number,sha256))''',
'''CREATE TABLE IF NOT EXISTS jq_price_revisions(id INTEGER PRIMARY KEY,instrument_id INTEGER NOT NULL REFERENCES instruments(id),traded_on TEXT NOT NULL,raw_json TEXT NOT NULL,sha256 TEXT NOT NULL,fetched_at TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES sources(id),UNIQUE(instrument_id,traded_on,sha256))''',
'''CREATE TABLE IF NOT EXISTS investment_sync_units(endpoint TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('complete','skipped','failed')),reason TEXT,rows_received INTEGER NOT NULL DEFAULT 0,rows_saved INTEGER NOT NULL DEFAULT 0,finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(endpoint,day))''',
'''CREATE TABLE IF NOT EXISTS investment_import_runs(id INTEGER PRIMARY KEY,endpoint TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,rows_received INTEGER NOT NULL DEFAULT 0,rows_saved INTEGER NOT NULL DEFAULT 0,reason TEXT,recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS investment_jobs(id TEXT PRIMARY KEY,requested_by TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,finished_at TEXT)''',
'''CREATE TABLE IF NOT EXISTS investment_relationships(id INTEGER PRIMARY KEY,from_instrument_id INTEGER NOT NULL REFERENCES instruments(id),to_instrument_id INTEGER NOT NULL REFERENCES instruments(id),kind TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES sources(id),observed_on TEXT NOT NULL,details_json TEXT NOT NULL DEFAULT '{}',UNIQUE(from_instrument_id,to_instrument_id,kind,source_id,observed_on))''',
'''CREATE TABLE IF NOT EXISTS investment_events(id INTEGER PRIMARY KEY,instrument_id INTEGER REFERENCES instruments(id),occurred_at TEXT NOT NULL,kind TEXT NOT NULL,description TEXT NOT NULL,source_id INTEGER NOT NULL REFERENCES sources(id),UNIQUE(instrument_id,occurred_at,kind,source_id))''',
'''CREATE TABLE IF NOT EXISTS investment_event_impacts(event_id INTEGER NOT NULL REFERENCES investment_events(id),instrument_id INTEGER NOT NULL REFERENCES instruments(id),hypothesis TEXT NOT NULL,evidence_source_id INTEGER NOT NULL REFERENCES sources(id),PRIMARY KEY(event_id,instrument_id,evidence_source_id))''',
'''CREATE TABLE IF NOT EXISTS investment_indicators(instrument_id INTEGER NOT NULL REFERENCES instruments(id),traded_on TEXT NOT NULL,method TEXT NOT NULL,value REAL NOT NULL,computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(instrument_id,traded_on,method))''',
'''CREATE INDEX IF NOT EXISTS ix_jq_master ON jq_instrument_snapshots(instrument_id,effective_on DESC,fetched_at DESC)''',
'''CREATE INDEX IF NOT EXISTS ix_jq_financials ON jq_financial_disclosures(instrument_id,disclosed_at DESC)''',
'''CREATE INDEX IF NOT EXISTS ix_investment_prices ON prices(instrument_id,traded_on DESC)''',
'''CREATE INDEX IF NOT EXISTS ix_investment_runs ON investment_import_runs(recorded_at DESC)''',
]


def migrate(path=None):
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    with connect(p) as c:
        c.execute('PRAGMA journal_mode=WAL')
    # Execute DDL one statement at a time: executescript would commit implicitly.
    with transaction(p) as c:
        for table, ddl in CORE.items():
            exists = c.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table',table)).fetchone()
            if not exists:
                c.execute(ddl)
            columns = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            if not REQUIRED[table] <= columns:
                raise RuntimeError('Incompatible existing schema: '+table+'; migrate separately from a backup')
        for ddl in DDL:
            c.execute(ddl)
        c.execute('INSERT OR IGNORE INTO investment_schema_versions(version) VALUES(1)')
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Database foreign-key integrity check failed')
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database integrity check failed')


def ticker(value):
    value = value.strip().upper()
    if not re.fullmatch(r'[0-9A-Z]{4}', value):
        raise ValueError('Expected a four-character Japanese stock code')
    return value


def day(value):
    return date.fromisoformat(value).isoformat()


def bounded(value, maximum=250):
    if isinstance(value,bool) or not 1 <= value <= maximum:
        raise ValueError('Limit out of range')
    return value


def search(query, limit=20, path=None):
    if not 1 <= len(query) <= 80:
        raise ValueError('Search must contain 1 to 80 characters')
    pattern = '%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    with connect(path) as c:
        return [dict(r) for r in c.execute('''SELECT i.id,i.ticker,i.exchange,s.name,s.sector,s.market FROM instruments i
        LEFT JOIN jq_instrument_snapshots s ON s.rowid=(SELECT s2.rowid FROM jq_instrument_snapshots s2 WHERE s2.instrument_id=i.id ORDER BY effective_on DESC,fetched_at DESC,rowid DESC LIMIT 1)
        WHERE i.country='JP' AND (i.ticker LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\') ORDER BY i.ticker LIMIT ?''', (pattern,pattern,bounded(limit,100)))]


def prices(code, limit=30, path=None):
    with connect(path) as c:
        return [dict(r) for r in c.execute('''SELECT p.traded_on,p.open,p.high,p.low,p.close,p.volume,p.adj_close,p.available_at FROM prices p JOIN instruments i ON i.id=p.instrument_id WHERE i.ticker=? AND i.country='JP' ORDER BY p.traded_on DESC LIMIT ?''',(ticker(code),bounded(limit)))]


def financials(code, limit=20, path=None):
    with connect(path) as c:
        rows=c.execute('''SELECT d.disclosure_number,d.disclosed_at,d.period_end,d.document_type,d.raw_json,d.sha256,d.fetched_at FROM jq_financial_disclosures d JOIN instruments i ON i.id=d.instrument_id WHERE i.ticker=? AND i.country='JP' ORDER BY d.disclosed_at DESC,d.id DESC LIMIT ?''',(ticker(code),bounded(limit,100)))
        return [{**dict(r),'raw_json':json.loads(r['raw_json'])} for r in rows]


def indicators(code, path=None):
    # Adjusted closes avoid false signals from stock splits. RSI uses a simple
    # 14-session window (Cutler RSI), explicitly not Wilder smoothing.
    rows=prices(code,250,path)
    values=[r['adj_close'] for r in reversed(rows)]
    if not values or any(v is None or not math.isfinite(v) or v <= 0 for v in values):
        return {'ticker':ticker(code),'status':'insufficient_adjusted_prices','observations':len(values)}
    result={'ticker':ticker(code),'date':rows[0]['traded_on'],'basis':'adjusted_close','observations':len(values)}
    for n in (5,25,75):
        result['sma_'+str(n)]=sum(values[-n:])/n if len(values)>=n else None
    result['cutler_rsi_14']=None
    if len(values)>=15:
        changes=[b-a for a,b in zip(values[-15:-1],values[-14:])]
        gains=sum(max(x,0) for x in changes); losses=sum(max(-x,0) for x in changes)
        result['cutler_rsi_14']=100*gains/(gains+losses) if gains+losses else 50.0
    return result


def statistics(path=None):
    with connect(path) as c:
        counts={t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('instruments','prices','jq_financial_disclosures','investment_relationships','investment_events')}
        return {'counts':counts,'latest_price_date':c.execute('SELECT MAX(traded_on) FROM prices').fetchone()[0], 'recent_updates':[dict(r) for r in c.execute('SELECT * FROM investment_sync_units ORDER BY finished_at DESC LIMIT 30')]}


def persist_indicators(path,traded_on):
    with connect(path) as c:
        codes=[(r['id'],r['ticker']) for r in c.execute("SELECT DISTINCT i.id,i.ticker FROM instruments i JOIN prices p ON p.instrument_id=i.id WHERE i.country='JP' AND p.traded_on=?",(traded_on,))]
    prepared=[]
    for iid,code in codes:
        result=indicators(code,path)
        if 'date' not in result:continue
        for method in ('sma_5','sma_25','sma_75','cutler_rsi_14'):
            if result.get(method) is not None:prepared.append((iid,result['date'],method,result[method]))
    with transaction(path) as c:
        c.executemany("INSERT INTO investment_indicators(instrument_id,traded_on,method,value) VALUES(?,?,?,?) ON CONFLICT(instrument_id,traded_on,method) DO UPDATE SET value=excluded.value,computed_at=CURRENT_TIMESTAMP",prepared)
