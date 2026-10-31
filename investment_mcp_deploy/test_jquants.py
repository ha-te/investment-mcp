import json, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import jquants
class FakeClient:
    def get(self,endpoint,params):
        d=params['date']
        return [{'Date':d,'Code':'72030','O':100,'H':105,'L':98,'C':102,'Vo':1000,'AdjC':102},
                {'Date':d,'Code':'43070','O':None,'H':None,'L':None,'C':None,'Vo':None}]
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=str(Path(self.tmp.name)/'test.db')
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE instruments(id INTEGER PRIMARY KEY,ticker TEXT,country TEXT); CREATE TABLE sources(id INTEGER PRIMARY KEY,publisher TEXT,url TEXT UNIQUE,source_type TEXT,license_notes TEXT,retrieved_at TEXT); CREATE TABLE prices(instrument_id INTEGER,traded_on TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,adj_close REAL,available_at TEXT,provider_source_id INTEGER,PRIMARY KEY(instrument_id,traded_on)); INSERT INTO instruments VALUES(1,"7203","JP");')
    def tearDown(self):self.tmp.cleanup()
    def test_preview(self):
        r=jquants.sync_day('2024-01-04',self.db,FakeClient())
        self.assertEqual((r['valid_trades'],r['no_trade'],r['saved']),(1,1,0))
    def test_commit_idempotent(self):
        self.assertEqual(jquants.sync_day('2024-01-04',self.db,FakeClient(),False)['saved'],1)
        self.assertEqual(jquants.sync_day('2024-01-04',self.db,FakeClient(),False)['saved'],0)
    def test_unknown_block(self):
        with sqlite3.connect(self.db) as c:c.execute('DELETE FROM instruments')
        r=jquants.sync_day('2024-01-04',self.db,FakeClient(),False)
        self.assertEqual(r['status'],'incomplete_unmapped_codes')
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT count(*) FROM prices').fetchone()[0],0)
    def test_normalize(self):
        self.assertEqual(jquants.normalized_code('186A0'),'186A')
        self.assertEqual(jquants.normalized_code('72030'),'7203')
    def test_missing_key(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(RuntimeError):jquants.key()
    def test_client_pagination(self):
        calls=[]
        class Response:
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(self.data).encode()
        def opener(req,timeout):
            calls.append(req.full_url)
            return Response({'data':[{'i':len(calls)}],**({'pagination_key':'next'} if len(calls)==1 else {})})
        client=jquants.Client(api_key='dummy',interval=0,opener=opener)
        self.assertEqual(len(client.get('/equities/bars/daily',{'date':'2024-01-04'})),2)
        self.assertIn('pagination_key=next',calls[1])
if __name__=='__main__':unittest.main()
