"""Owner shell maintenance: online SQLite backup, migration and integrity check."""
import argparse
import os
import sqlite3
from pathlib import Path
import database as db


def backup(source,destination):
    dest=Path(destination)
    if dest.exists():raise ValueError('Backup destination already exists')
    dest.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create, mode 0600; never overwrite a user's existing backup.
    fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with db.connect(source) as src,sqlite3.connect(dest) as target:
            src.backup(target,pages=256,sleep=0.1)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Backup integrity check failed')
    except BaseException:
        dest.unlink(missing_ok=True);raise
    return dest


def main():
    p=argparse.ArgumentParser()
    p.add_argument('operation',choices=['migrate','check','backup'])
    p.add_argument('--destination')
    args=p.parse_args()
    if args.operation=='backup':
        if not args.destination:p.error('--destination is required')
        backup(db.db_path(),args.destination);print('Backup created and integrity checked')
    elif args.operation=='migrate':db.migrate();print('Migration complete')
    else:
        with db.connect() as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():
                raise SystemExit('Integrity check failed')
        print('Integrity check passed')
if __name__=='__main__':main()
