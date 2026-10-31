"""HTTPS ingress -> OAuth JWT resource server. No proxy flag grants access."""
import logging
import os
from pathlib import Path
from starlette.responses import JSONResponse
import database as db
from app import create_mcp
from security import Limits


def persistent_mount(volume):
    # ismount() alone misses Docker bind mounts on the same device. Linux
    # mountinfo is authoritative for bind mounts too; reject memory filesystems.
    if str(volume)=='/':return False
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before,after=line.split(' - ',1)
            import re
            target=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),before.split()[4])
            if target==str(volume):return after.split()[0] not in ('tmpfs','ramfs')
        return False
    except OSError:return False


def create_app(verifier=None,check_volume=True):
    path=db.db_path().resolve()
    if check_volume:
        volume=Path(os.environ.get('INVESTMENT_DATA_DIR','/data')).resolve()
        if not path.is_relative_to(volume) or not persistent_mount(volume):
            raise ValueError('INVESTMENT_DB must reside on the mounted persistent data volume')
    mcp=create_mcp(remote=True,verifier=verifier,path=path)
    @mcp.custom_route('/healthz',methods=['GET'])
    async def health(request):
        try:
            with db.connect(path) as c:c.execute('SELECT 1 FROM investment_schema_versions').fetchone()
            return JSONResponse({'status':'ok'})
        except Exception:return JSONResponse({'status':'unavailable'},503)
    return Limits(mcp.streamable_http_app())


if __name__=='__main__':
    import uvicorn
    # Avoid SDK tracebacks containing database paths or user-controlled inputs.
    logging.getLogger('mcp').setLevel(logging.CRITICAL)
    uvicorn.run(create_app(),host='0.0.0.0',port=int(os.getenv('PORT','8000')),proxy_headers=False,access_log=False,log_level='warning')
