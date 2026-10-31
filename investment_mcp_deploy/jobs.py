"""Single-host persistent jobs with an OS lock shared by CLI, MCP and scheduler."""
from __future__ import annotations
import fcntl
import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import database as db
from sync import sync_range


class Jobs:
    def __init__(self,path=None):
        self.path=str(path or db.db_path())
        self.stop=threading.Event()
        self.threads=[]

    def _lock(self):
        lock=open(self.path+'.sync.lock','a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('An update is already running') from None
        return lock

    def submit(self,start,end,subject,force=False,background=True,client=None):
        from datetime import date
        a,b=date.fromisoformat(db.day(start)),date.fromisoformat(db.day(end))
        if b<a or (b-a).days>30:raise ValueError('Choose at most 31 calendar days')
        lock=self._lock()
        job=uuid.uuid4().hex
        try:
            with db.transaction(self.path) as c:
                c.execute("UPDATE investment_jobs SET status='interrupted',finished_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running')")
                c.execute("INSERT INTO investment_jobs(id,requested_by,start_date,end_date,status) VALUES(?,?,?,?,'queued')",(job,subject,start,end))
        except BaseException:
            lock.close();raise
        if background:
            t=threading.Thread(target=self._run,args=(job,start,end,force,client,lock),daemon=True)
            try:t.start()
            except BaseException:lock.close();raise
            self.threads.append(t)
        else:self._run(job,start,end,force,client,lock)
        return {'job_id':job}

    def _run(self,job,start,end,force,client,lock):
        try:
            with db.transaction(self.path) as c:c.execute("UPDATE investment_jobs SET status='running' WHERE id=?",(job,))
            result=sync_range(start,end,self.path,client=client,force=force)
            with db.transaction(self.path) as c:c.execute('UPDATE investment_jobs SET status=?,result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?',(result['status'],json.dumps(result),job))
        except Exception:
            with db.transaction(self.path) as c:c.execute("UPDATE investment_jobs SET status='failed',result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",('{"reason":"update_failed"}',job))
        finally:lock.close()

    def status(self,job):
        if len(job)!=32 or any(ch not in '0123456789abcdef' for ch in job):raise ValueError('Invalid job id')
        with db.connect(self.path) as c:
            row=c.execute('SELECT id,start_date,end_date,status,result_json,created_at,finished_at FROM investment_jobs WHERE id=?',(job,)).fetchone()
        if not row:raise ValueError('Unknown job')
        result=dict(row)
        result['result_json']=json.loads(result['result_json']) if result['result_json'] else None
        return result

    def scheduled_tick(self,now=None):
        now=now or datetime.now(ZoneInfo('Asia/Tokyo'))
        if now.weekday()>4 or now.hour<18:return
        with db.connect(self.path) as c:
            exists=c.execute("SELECT 1 FROM investment_jobs WHERE requested_by='scheduler' AND start_date=? AND end_date=? AND (status IN ('queued','running','complete','partial') OR created_at>=datetime('now','-1 hour'))",((now.date()-timedelta(days=6)).isoformat(),now.date().isoformat())).fetchone()
        if exists:return
        try:self.submit((now.date()-timedelta(days=6)).isoformat(),now.date().isoformat(),'scheduler',force=True)
        except ValueError:pass  # another process owns the same DB; retry next tick

    def start_scheduler(self):
        def loop():
            while not self.stop.is_set():
                try:self.scheduled_tick()
                except Exception:pass  # No exception text / upstream response in logs.
                self.stop.wait(60)
        t=threading.Thread(target=loop,daemon=True);t.start();self.threads.append(t)

    def close(self):
        self.stop.set()
        # Do not release job locks while worker threads still write. The process
        # owns them until exit; restart marks unfinished jobs interrupted.

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--force',action='store_true')
    args=p.parse_args()
    db.migrate()
    jobs=Jobs();job=jobs.submit(args.start,args.end,'local-owner',force=args.force,background=False)
    result=jobs.status(job['job_id']);print(json.dumps(result))
    raise SystemExit(0 if result['status']=='complete' else 1)
