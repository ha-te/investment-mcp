import os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import server
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();server.DB=Path(self.tmp.name)/'db.sqlite'
  with sqlite3.connect(server.DB) as c:
   c.executescript('CREATE TABLE instruments(id INTEGER PRIMARY KEY,ticker TEXT,exchange TEXT,country TEXT);CREATE TABLE sources(id INTEGER PRIMARY KEY,publisher TEXT,url TEXT UNIQUE,source_type TEXT,license_notes TEXT,retrieved_at TEXT);CREATE TABLE prices(instrument_id INTEGER,traded_on TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,adj_close REAL,available_at TEXT,provider_source_id INTEGER,PRIMARY KEY(instrument_id,traded_on));CREATE TABLE financial_facts(id INTEGER PRIMARY KEY);INSERT INTO instruments VALUES(1,"7203","TSE","JP");')
 def tearDown(self):self.tmp.cleanup()
 def test_status(self):self.assertEqual(server.status()['counts']['instruments'],1)
 def test_dry_run(self):
  r=server.ingest_price_csv('ticker,date,open,high,low,close,volume\n7203,2026-10-20,100,110,95,105,1000',dry_run=True)
  self.assertEqual(r['rows_valid'],1);self.assertEqual(server.status()['counts']['prices'],0)
 def test_commit_and_idempotency(self):
  csv='ticker,date,open,high,low,close,volume\n7203,2026-10-20,100,110,95,105,1000'
  self.assertEqual(server.ingest_price_csv(csv)['saved'],1)
  self.assertEqual(server.ingest_price_csv(csv)['saved'],0)
 def test_bad_ohlc(self):
  with self.assertRaises(ValueError):server.ingest_price_csv('ticker,date,open,high,low,close,volume\n7203,2026-10-20,100,90,95,105,1000')
 def test_unknown(self):
  r=server.ingest_price_csv('ticker,date,open,high,low,close,volume\nXXXX,2026-10-20,100,110,95,105,1000')
  self.assertEqual(r['unknown_tickers'],['XXXX'])
 def test_blocked_hosts(self):
  with self.assertRaises(ValueError):server.request_bytes('http://data.sec.gov/x',{'data.sec.gov'})
  with self.assertRaises(ValueError):server.request_bytes('https://example.org/x',{'data.sec.gov'})
 def test_sec_contact(self):
  with self.assertRaises(ValueError):server.fetch_sec_companyfacts('320193','AAPL')
if __name__=='__main__':unittest.main()
