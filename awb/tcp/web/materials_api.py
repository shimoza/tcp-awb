"""Owner-side, copy-only OBS imports. Names and originals never cross the work boundary.

The buckets are read through the owner's key service (KeySource): this service holds no key of its own."""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).

import argparse
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from awb import config, projects, register, intake, check, extract, gate
from awb.tcp import keys

SOCKET='/run/awb-materials.sock'
PUBLISH_SOCKET='/run/awb-project-create.sock'
STATE='/var/lib/awb-materials'
CODE=re.compile(r'tcp-[a-z2-7]{4}')
IDENT=re.compile(r'M-[A-Z]{24}')
IDENT_FILE=re.compile(r'M-[A-Z]{24}\.md')
PROJECT_COPY='Ready text copy from the project folder.'
TOO_LARGE='Extracted text is too large. Split this document into smaller files.'
REQ=re.compile(r'[a-f0-9]{32}')
MAX_FILE=25*1024*1024
MAX_TEXT=256*1024
MAX_OBJECTS=2000
SUPPORTED={'.md','.txt','.csv','.tsv','.json','.yaml','.yml','.pdf','.docx','.xlsx','.pptx','.odt','.ods','.odp'}
# the console's upload (2026-10-09): the file lands in the project's folder in/ of the owner bucket under an id and
# its extension, never its name, and the project session takes it from there (awb inbox take); nothing is imported
UPLOAD_STORED='The file is in the project folder in/. The session takes it with: take the files from in'
UPLOAD_TYPE='The file type or size is unsupported.'
UPLOAD_FAILED='The file could not be stored. Try again later.'

# D-F3 = no (his decision of 2026-10-08): no file name crosses Cloudflare. The web answers show a file as its id and
# its extension; the owner's terminal and the internal reads keep the names.
NAMES_CROSS=False
EXT=re.compile(r'\.[a-z0-9]{1,8}')


def extension(name):
    ext=Path(str(name or '')).suffix.lower()
    return ext if EXT.fullmatch(ext) else ''


def without_names(answer,kind):
    """A web answer of the materials service in the codes-only shape: every file name becomes the item's id with
    its extension (the history and one item) or `file N` with its extension (a source listing)."""
    if NAMES_CROSS:return answer
    if kind=='browse':
        files=[dict(f,name='file %d%s'%(i+1,extension(f.get('name'))),extension=extension(f.get('name')))
               for i,f in enumerate(answer.get('files',[]))]
        return dict(answer,files=files)
    if kind=='history':
        items=[dict(i,name=i['id']+extension(i.get('name')),filename=i['id']+extension(i.get('filename')),
                    extension=extension(i.get('name'))) for i in answer.get('items',[])]
        return dict(answer,items=items)
    return dict(answer,filename=answer['id']+extension(answer.get('filename')),extension=extension(answer.get('filename')))


class Problem(Exception):
    def __init__(self,status,message): self.status=status;self.message=message

def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
def identifier():return 'M-'+''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ') for _ in range(24))
def digest(data):return hashlib.sha256(data).hexdigest()

def local_call(path,route,method='GET',data=None,limit=2*1024*1024):
    c=http.client.HTTPConnection('localhost',timeout=30)
    c.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);c.sock.settimeout(30);c.sock.connect(path)
    try:
        c.request(method,route,body=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        r=c.getresponse();raw=r.read(limit+1)
        if len(raw)>limit:raise Problem(503,'The local response exceeds the size limit.')
        result=json.loads(raw)
        if r.status>=400:raise Problem(r.status,result.get('error','The local service is unavailable.'))
        return result
    finally:c.close()

KEYS_LOCKED='The key service is locked. The owner unlocks it in his terminal.'
KEYS_DOWN='The key service is unavailable. Try again later.'
NOT_READ='The bucket could not be read. Try again later.'


class KeySource:
    """One bucket, read through the owner's key service (its web mode, op web_read). This process holds no key and
    signs nothing; the key service checks every folder, logs every read and never writes or deletes. A refusal of
    the key service reaches the browser with the same text as when this service read the bucket itself."""

    def __init__(self, which, request=None, sock=None):
        self.which, self.request, self.sock = which, request or keys.request, sock

    def _ask(self, obj, download=None, too_many=None):
        try:
            answer = self.request(self.sock or keys.call_socket(), {'op': 'web_read', 'bucket': self.which, **obj},
                                  download=download)
        except keys.KeysError:
            raise Problem(503, KEYS_DOWN) from None
        if not isinstance(answer, dict):
            raise Problem(503, NOT_READ)
        if answer.get('ok'):
            return answer
        kind, status = answer.get('kind'), answer.get('status')
        if kind == 'locked':
            raise Problem(503, KEYS_LOCKED)
        if kind == 'setup':
            raise Problem(503, 'The AWB bucket connection is not configured.')
        if kind == 'changed':
            raise Problem(409, 'The source changed. Refresh the file list and import its current version.')
        if kind == 'too_large':
            raise Problem(413, 'The file exceeds the import limit.')
        if kind == 'too_many' and too_many:
            raise Problem(413, too_many)
        if kind == 'inconsistent':
            raise Problem(503, 'The bucket returned an inconsistent listing.')
        if kind == 'http' and isinstance(status, int) and not isinstance(status, bool) and status > 0:
            raise Problem(503, 'Bucket read access is unavailable (HTTP %d).' % status)
        raise Problem(503, NOT_READ)

    def months(self):
        answer = self._ask({'what': 'months'}, too_many='The customer bucket layout needs a narrower binding.')
        return [m for m in answer.get('months', []) if isinstance(m, str)]

    def listing(self, prefix):
        answer = self._ask({'what': 'list', 'prefix': prefix},
                           too_many='This source has too many files. Narrow the source folder first.')
        return list(answer.get('objects', []))

    def put_in(self, code, extension, path):
        """The uploaded file into the project's folder in/ of the owner bucket, through the key service (op
        web_put_in, the owner's process only): the id the object got."""
        try:
            answer = self.request(self.sock or keys.call_socket(), {'op': 'web_put_in', 'project': code,
                                                                     'extension': extension}, upload=path)
        except keys.KeysError:
            raise Problem(503, KEYS_DOWN) from None
        if not isinstance(answer, dict):
            raise Problem(503, UPLOAD_FAILED)
        if answer.get('ok') and isinstance(answer.get('id'), str):
            return answer['id']
        if answer.get('kind') == 'locked':
            raise Problem(503, KEYS_LOCKED)
        if answer.get('kind') == 'too_large':
            raise Problem(413, UPLOAD_TYPE)
        raise Problem(503, UPLOAD_FAILED)

    def read(self, key, etag, to):
        """The object at the version `etag` into `to`; another version is refused as changed."""
        answer = self._ask({'what': 'get', 'key': key, 'etag': etag, 'limit': MAX_FILE}, download=to)
        if not Path(to).is_file() or Path(to).stat().st_size != answer.get('size'):
            raise Problem(503, NOT_READ)
        return {'ETag': str(answer.get('etag', ''))}, b''


class Sources:
    """The two buckets through the key service: the lab inbox for a task description (brief), the owner's inbox and
    the project's own in/ folders for customer material."""

    def __init__(self, request=None, sock=None):
        self.request, self.sock = request, sock

    def client(self, source):
        return KeySource('lab' if source == 'brief' else 'owner', self.request, self.sock)

    def put_in(self, code, extension, path):
        return KeySource('owner', self.request, self.sock).put_in(code, extension, path)

    def prefixes(self, project, source):
        if source == 'brief':
            return ['inbox/']
        # Existing AWB project folders, including folders kept under an earlier month.
        months = {project.created[:7]} | {m.rstrip('/') for m in self.client(source).months()}
        if len(months) > 120:
            raise Problem(413, 'The customer bucket layout exceeds the supported limit.')
        return ['inbox/'] + [m + '/' + project.code + '/in/' for m in sorted(months)]


class Store:
    def __init__(self,paths=None,state=STATE,sources=None,publisher=None):
        self.p=paths or config.paths();self.root=Path(state);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.sources=sources or Sources();self.publisher=publisher or self.publish
        self.lock=threading.Lock();self.list_lock=threading.Lock()
        self.keyfile=self.root/'digest.key'
        if not self.keyfile.exists():
            with open(os.open(self.keyfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:f.write(secrets.token_bytes(32))
        self.key=self.keyfile.read_bytes()
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, project TEXT, customer TEXT, source TEXT, object_key TEXT, etag TEXT, size INTEGER, modified TEXT, seen TEXT);
CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, project TEXT, fingerprint TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, project TEXT, customer TEXT, object_id TEXT, source TEXT, object_key TEXT, etag TEXT, size INTEGER, name TEXT, state TEXT, message TEXT, created TEXT, version INTEGER, sha TEXT, text_sha TEXT, filename TEXT);
CREATE INDEX IF NOT EXISTS import_project ON imports(project,created);
''')
    def db(self):
        c=sqlite3.connect(self.root/'imports.sqlite3',timeout=10);c.row_factory=sqlite3.Row;return c
    def project(self,code,active=False):
        if not CODE.fullmatch(code or ''):raise Problem(404,'Project not found.')
        p=next((x for x in projects.load(self.p) if x.code==code and x.state!='deleted'),None)
        if not p:raise Problem(404,'Project not found.')
        if active and p.state!='active':raise Problem(409,'Reopen this project before importing files.')
        if p.customer!='none' and not any(e.code==p.customer and e.status=='active' for e in register.load(self.p.register)):
            raise Problem(409,'The customer is unavailable or retired.')
        return p
    def source_id(self,code,source,key):return hmac.new(self.key,(code+'\0'+source+'\0'+key).encode(),hashlib.sha256).hexdigest()
    def history(self,code):
        p=self.project(code)
        with self.db() as c:rows=[dict(r) for r in c.execute('SELECT * FROM imports WHERE project=? ORDER BY created DESC,rowid DESC LIMIT 500',(code,))]
        items=[{k:r[k] for k in ('id','name','source','state','message','created','version','filename','size')} for r in rows]
        # text copies that reached input/ another way (the owner's intake on the command line, a take of the
        # exchange): customer material in a project with a customer, a task description in one without
        source='brief' if p.customer=='none' else 'customer'
        for ident,name,_,size,modified in self.project_files(p):
            large=size>MAX_TEXT
            items.append({'id':ident,'name':name,'source':source,'state':'held' if large else 'ready',
                          'message':TOO_LARGE if large else PROJECT_COPY,'created':modified,'version':1,
                          'filename':name,'size':size})
        return {'items':items, 'busy':any(r['state'] in ('queued','processing','publishing') for r in rows)}
    def project_file_id(self,code,name):
        """A stable id in the shape of an import for a file of the project's input/ folder."""
        mac=hmac.new(self.key,('input\0'+code+'\0'+name).encode(),hashlib.sha256).digest()
        return 'M-'+''.join(chr(65+b%26) for b in mac[:24])
    def project_files(self,p):
        """The Markdown and text files directly in the project's input/ folder that this service did not import:
        (id, name, path, size, modified). Links, folders, the public intake report and the service's own copies
        are left out; a project folder that is not the registered one is not read."""
        if Path(p.path)!=self.p.projects_root/p.code:return []
        folder=Path(p.path)/'input'
        try:names=sorted(os.listdir(folder))
        except OSError:return []
        out=[]
        for name in names:
            if name=='intake-report.md' or IDENT_FILE.fullmatch(name) or Path(name).suffix.lower() not in ('.md','.txt'):continue
            try:st=os.lstat(folder/name)
            except OSError:continue
            if not stat.S_ISREG(st.st_mode):continue
            out.append((self.project_file_id(p.code,name),name,folder/name,st.st_size,
                        dt.datetime.fromtimestamp(st.st_mtime,dt.timezone.utc).isoformat(timespec='seconds')))
        return out
    def browse(self,code,source):
        p=self.project(code,True)
        if source not in ('brief','customer'):raise Problem(400,'Choose a source.')
        if source=='customer' and p.customer=='none':raise Problem(409,'Select a customer for this project before importing customer material.')
        if not self.list_lock.acquire(blocking=False):raise Problem(429,'Another source is being listed. Try again shortly.')
        try:
            c=self.sources.client(source);rows=[]
            for prefix in self.sources.prefixes(p,source):rows.extend(c.listing(prefix))
            if len(rows)>MAX_OBJECTS:raise Problem(413,'The source has too many files.')
            result=[]
            with self.db() as db:
                for r in rows:
                    if not r['etag'] or len(r['key'])>2048 or any(ord(x)<32 for x in r['key']):continue
                    oid=self.source_id(code,source,r['key']);name=r['key'].rsplit('/',1)[-1]
                    db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?,?,?,?)',(oid,code,p.customer,source,r['key'],r['etag'],r['size'],r['modified'],now()))
                    old=db.execute("SELECT etag,state FROM imports WHERE project=? AND object_id=? AND state IN ('ready','duplicate','queued','processing','publishing') ORDER BY rowid DESC LIMIT 1",(code,oid)).fetchone()
                    status=('imported' if old['state'] in ('ready','duplicate') else 'processing') if old and old['etag']==r['etag'] else 'updated' if old else 'new'
                    supported=Path(name).suffix.lower() in SUPPORTED and 0<r['size']<=MAX_FILE
                    result.append({'id':oid,'name':name,'etag':r['etag'],'size':r['size'],'modified':r['modified'],'status':status,'supported':supported,'location':'Unassigned inbox' if r['key'].startswith('inbox/') else 'Project folder'})
            return {'source':source,'files':result,'limit_mb':MAX_FILE//1024//1024}
        finally:self.list_lock.release()
    def upload(self,code,extension,path):
        """One file of the console into the project's in/ (any project, with or without a customer): no intake here,
        the session's take runs it on the owner side. The answer names no file name."""
        self.project(code,True)
        size=Path(path).stat().st_size
        if extension not in SUPPORTED or not 0<size<=MAX_FILE:raise Problem(413,UPLOAD_TYPE)
        ident=self.sources.put_in(code,extension,path)
        return {'id':ident,'extension':extension,'size':size,'folder':'in/','message':UPLOAD_STORED}
    def queue(self,code,data):
        p=self.project(code,True)
        if not isinstance(data,dict) or set(data)!={'request_id','files'} or not REQ.fullmatch(str(data.get('request_id',''))):raise Problem(400,'Invalid import request.')
        files=data['files']
        if not isinstance(files,list) or not 1<=len(files)<=10 or any(not isinstance(f,dict) or set(f)!={'id','etag'} or not re.fullmatch(r'[a-f0-9]{64}',str(f['id'])) or not isinstance(f['etag'],str) for f in files):raise Problem(400,'Select between 1 and 10 files.')
        if len({f['id'] for f in files})!=len(files):raise Problem(400,'Select each file once.')
        fp=digest(json.dumps(sorted(files,key=lambda x:x['id']),sort_keys=True).encode())
        with self.lock,self.db() as db:
            prior=db.execute('SELECT * FROM requests WHERE id=?',(data['request_id'],)).fetchone()
            if prior:
                if prior['project']!=code or prior['fingerprint']!=fp:raise Problem(409,'This request belongs to another import.')
                return json.loads(prior['result'])
            chosen=[]
            for f in files:
                row=db.execute('SELECT * FROM objects WHERE id=? AND project=?',(f['id'],code)).fetchone()
                if not row or row['customer']!=p.customer or row['etag']!=f['etag']:raise Problem(409,'The source selection changed. Refresh the file list.')
                if row['source']=='customer' and p.customer=='none':raise Problem(409,'A customer is required.')
                if Path(row['object_key']).suffix.lower() not in SUPPORTED or not 0<row['size']<=MAX_FILE:raise Problem(413,'The selected file type or size is unsupported.')
                chosen.append(row)
            pending=db.execute("SELECT count(*) FROM imports WHERE state IN ('queued','processing','publishing')").fetchone()[0]
            if pending+len(chosen)>30:raise Problem(429,'The import queue is full. Wait for current files to finish.')
            ids=[]
            for row in chosen:
                prior=db.execute("SELECT id FROM imports WHERE project=? AND object_id=? AND etag=? AND state IN ('ready','duplicate','queued','processing','publishing') ORDER BY rowid DESC LIMIT 1",(code,row['id'],row['etag'])).fetchone()
                if prior:ids.append(prior['id']);continue
                uncertain=db.execute("SELECT id FROM imports WHERE project=? AND object_id=? AND etag=? AND state='failed' AND text_sha IS NOT NULL ORDER BY rowid DESC LIMIT 1",(code,row['id'],row['etag'])).fetchone()
                if uncertain:
                    ids.append(uncertain['id']);db.execute("UPDATE imports SET state='publishing',message='Checking publication of the saved working copy.' WHERE id=?",(uncertain['id'],));continue
                ident=identifier();ids.append(ident)
                version=db.execute("SELECT coalesce(max(version),0)+1 FROM imports WHERE project=? AND object_id=? AND state='ready'",(code,row['id'])).fetchone()[0]
                db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,code,p.customer,row['id'],row['source'],row['object_key'],row['etag'],row['size'],Path(row['object_key']).name,'queued','Waiting for processing.',now(),version,None,None,ident+'.md'))
            result={'imports':ids};db.execute('INSERT INTO requests VALUES(?,?,?,?)',(data['request_id'],code,fp,json.dumps(result)))
            return result
    def update(self,ident,**fields):
        with self.db() as c:c.execute('UPDATE imports SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),ident))
    def publish(self,row,text):
        return local_call(PUBLISH_SOCKET,'/internal/projects/'+row['project']+'/materials','POST',{'id':row['id'],'customer':row['customer'],'text':text,'sha256':digest(text.encode())})
    def checked(self,text):
        if len(text.encode())>MAX_TEXT:raise Problem(422,'Extracted text is too large. Split this document into smaller files.')
        if check.check_text(text,self.p.register) or any(gate.DETECTORS[k](text) for k in ('secret','private-key','token','homepath')):
            raise Problem(422,'The working copy contains protected values. Owner review is required.')
        return text
    def process(self,ident):
        with self.db() as db:row=db.execute('SELECT * FROM imports WHERE id=?',(ident,)).fetchone()
        if not row or row['state'] not in ('queued','publishing'):return
        row=dict(row)
        try:
            p=self.project(row['project'],True)
            if p.customer!=row['customer']:raise Problem(409,'The project customer changed. Import again with the correct customer.')
            stage=self.root/(ident+'.md')
            if row['state']=='publishing':
                text=stage.read_text();self.checked(text)
                if digest(text.encode())!=row['text_sha']:raise Problem(422,'The staged document changed. Owner review is required.')
                self.publisher(row,text);self.update(ident,state='ready',message='Ready text copy. Embedded images are not included.');return
            self.update(ident,state='processing',message='Downloading and checking the selected version.')
            tmp_root=self.p.vault/'tmp';tmp_root.mkdir(exist_ok=True,mode=0o700)
            with tempfile.TemporaryDirectory(prefix='web-import-',dir=tmp_root) as folder:
                original=Path(folder)/row['name']
                if original.name in ('','.','..') or '/' in original.name or '\\' in original.name:raise Problem(422,'The filename is unsupported.')
                c=self.sources.client(row['source']);headers,_=c.read(row['object_key'],etag=row['etag'],to=original)
                if original.stat().st_size!=row['size'] or next((v for k,v in headers.items() if k.lower()=='etag'),'').strip('"')!=row['etag']:raise Problem(409,'The source changed. Refresh the file list and import its current version.')
                sha=digest(original.read_bytes());self.update(ident,sha=sha)
                with self.db() as db:
                    prior=db.execute("SELECT id,version FROM imports WHERE project=? AND object_id=? AND sha=? AND state='ready' ORDER BY rowid DESC LIMIT 1",(row['project'],row['object_id'],sha)).fetchone()
                if prior:
                    self.update(ident,state='duplicate',version=prior['version'],message='The same content is already imported as '+prior['id']+'.');return
                if row['source']=='customer':
                    # Public outbox is isolated per job; only this job's approved copies can be published.
                    from dataclasses import replace
                    job_shared=self.root/'jobs'/ident;job_shared.mkdir(parents=True,mode=0o700,exist_ok=True)
                    job_paths=replace(self.p,shared=job_shared)
                    result=intake.run([original],row['customer'],job_paths)
                    if result.unsealed:raise Problem(422,'The original could not be sealed. Owner review is required before release.')
                    if not result.states or any(v not in ('ok','empty') for v in result.states.values()):raise Problem(422,'The document could not be read completely. Review it or provide a supported text version.')
                    approved=[f for f in result.outputs if f.name!='intake-report.md']
                    if not approved:raise Problem(422,'No usable text was extracted from this file.')
                    text='\n\n'.join(f.read_text() for f in approved)
                else:
                    with extract.temp_root(Path(folder)):
                        ex=extract.extract(original)
                    if ex.state!='ok' or ex.meta.get(extract.INCOMPLETE):raise Problem(422,'The document could not be read completely. Provide a supported text version.')
                    if check.check_file(original,self.p.register):raise Problem(422,'This task description may contain private information. Use the customer source or ask the owner to review it.')
                    # wipe mode (decision 16): a name the candidate rules recognise becomes a token, nothing holds
                    text=intake.wipe_text(ex.text,self.p)
                if not text.strip():raise Problem(422,'This file contains no readable text.')
                self.checked(text)
                with self.db() as db:version=db.execute("SELECT coalesce(max(version),0)+1 FROM imports WHERE project=? AND object_id=? AND state='ready'",(row['project'],row['object_id'])).fetchone()[0]
                self.update(ident,version=version)
                with open(stage,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
                os.chmod(stage,0o600)
                self.update(ident,state='publishing',text_sha=digest(text.encode()),message='Adding the checked working copy to the project.')
                self.publisher(row,text)
                self.update(ident,state='ready',message='Ready text copy. Embedded images are not included.')
        except Problem as e:self.update(ident,state='held' if e.status==422 else 'failed',message=e.message)
        except Exception:self.update(ident,state='failed',message='Import stopped. Check the connection and vault, then retry. Source files were preserved.')
    def text(self,code,ident):
        project=self.project(code)
        with self.db() as db:row=db.execute("SELECT * FROM imports WHERE project=? AND id=? AND state='ready'",(code,ident)).fetchone()
        if not row:
            for fid,name,path,size,modified in self.project_files(project):
                if fid!=ident:continue
                if size>MAX_TEXT:raise Problem(422,'The imported working copy is unavailable.')
                try:
                    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as fh:raw=fh.read(MAX_TEXT+1)
                    text=raw.decode('utf-8')
                except (OSError,UnicodeDecodeError):raise Problem(422,'The imported working copy is unavailable.') from None
                if len(raw)>MAX_TEXT:raise Problem(422,'The imported working copy is unavailable.')
                self.checked(text)
                return {'id':ident,'version':1,'filename':name,'imported':modified,'text':text}
            raise Problem(404,'The selected input is not ready in this project.')
        if row['customer']!=project.customer:raise Problem(409,'The project customer changed. This input cannot be used in its new context.')
        path=self.root/(ident+'.md')
        if path.is_symlink() or path.stat().st_size>MAX_TEXT:raise Problem(422,'The imported working copy is unavailable.')
        text=path.read_text()
        if digest(text.encode())!=row['text_sha']:raise Problem(422,'The imported working copy changed. Import it again before use.')
        self.checked(text)
        return {'id':ident,'version':row['version'],'filename':row['filename'],'imported':row['created'],'text':text}
    def worker(self):
        # A killed extraction is never reported ready. Staged copies can finish idempotent publication.
        with self.db() as db:db.execute("UPDATE imports SET state='failed',message='Processing was interrupted. Retry the import; source files were preserved.' WHERE state='processing'")
        while True:
            with self.db() as db:row=db.execute("SELECT id FROM imports WHERE state IN ('queued','publishing') ORDER BY rowid LIMIT 1").fetchone()
            if not row:time.sleep(1);continue
            try:
                subprocess.run([sys.executable,'-m','awb.tcp.web.materials_api','--state',str(self.root),'--process',row['id']],timeout=120,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
            except Exception:self.update(row['id'],state='failed',message='Processing timed out or stopped. Retry with a smaller supported document.')
            time.sleep(1)

class Handler(BaseHTTPRequestHandler):
    def setup(self):super().setup();self.connection.settimeout(30)
    def reply(self,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
        for k,v in {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store, private','Content-Length':str(len(raw)),'Connection':'close'}.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.dispatch()
    def do_POST(self):self.dispatch()
    def dispatch(self):
        try:
            uid=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
            url=urllib.parse.urlsplit(self.path);route=re.fullmatch(r'/api/projects/(tcp-[a-z2-7]{4})/materials(?:/(sources|imports|upload|M-[A-Z]{24}))?',url.path)
            inner=re.fullmatch(r'/internal/projects/(tcp-[a-z2-7]{4})/materials/(M-[A-Z]{24})',url.path)
            if inner and self.command=='GET' and uid in self.server.read_uids:return self.reply(200,self.server.store.text(*inner.groups()))
            if uid!=self.server.web_uid:raise Problem(403,'Not allowed.')
            if not route:raise Problem(404,'Not found.')
            code,action=route.groups()
            if self.command=='GET':
                if action=='sources':
                    q=urllib.parse.parse_qs(url.query,strict_parsing=True,max_num_fields=1)
                    return self.reply(200,without_names(self.server.store.browse(code,(q.get('source') or [''])[0]),'browse'))
                if action is None:return self.reply(200,without_names(self.server.store.history(code),'history'))
                if IDENT.fullmatch(action):return self.reply(200,without_names(self.server.store.text(code,action),'item'))
            if self.command=='POST' and action=='upload':
                return self.reply(201,self.server.store.upload(code,*self.received()))
            if self.command=='POST' and action=='imports':
                lengths=self.headers.get_all('Content-Length',[])
                if len(lengths)!=1 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise Problem(400,'Invalid request.')
                n=int(lengths[0])
                if not 0<n<=16384:raise Problem(413,'Request is too large.')
                raw=self.rfile.read(n)
                if len(raw)!=n:raise Problem(400,'Incomplete request.')
                return self.reply(202,self.server.store.queue(code,json.loads(raw)))
            raise Problem(405,'Method not allowed.')
        except Problem as e:self.reply(e.status,{'error':e.message})
        except (ValueError,TypeError):self.reply(400,{'error':'Invalid request.'})
        except Exception:self.reply(503,{'error':'The input service is unavailable. Check the bucket connection and vault.'})
    def received(self):
        """The bytes of an upload into a private file of the service: (extension, path). The browser sends the
        extension alone (X-AWB-Extension), never the file name."""
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)!=1 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/octet-stream':raise Problem(400,'Invalid request.')
        n=int(lengths[0]);ext=self.headers.get('X-AWB-Extension','').lower()
        if not EXT.fullmatch(ext) or ext not in SUPPORTED:raise Problem(413,UPLOAD_TYPE)
        if not 0<n<=MAX_FILE:raise Problem(413,UPLOAD_TYPE)
        tmp_root=self.server.store.p.vault/'tmp';tmp_root.mkdir(exist_ok=True,mode=0o700)
        fd,name=tempfile.mkstemp(prefix='web-upload-',dir=tmp_root);path=Path(name)
        self.uploaded=path
        with os.fdopen(fd,'wb') as out:
            got=0
            while got<n:
                chunk=self.rfile.read(min(1<<16,n-got))
                if not chunk:raise Problem(400,'Incomplete request.')
                out.write(chunk);got+=len(chunk)
        return ext,path
    def finish(self):
        try:super().finish()
        finally:
            up=getattr(self,'uploaded',None)
            if up is not None:Path(up).unlink(missing_ok=True)
    def log_message(self,*a):pass
class Server(ThreadingMixIn,HTTPServer):
    address_family=socket.AF_UNIX;daemon_threads=True
    def get_request(self):c,_=self.socket.accept();return c,('local',0)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--state',default=STATE);ap.add_argument('--process');args=ap.parse_args()
    store=Store(state=args.state)
    if args.process:
        if not IDENT.fullmatch(args.process):raise SystemExit(2)
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(90,90));resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        store.process(args.process);return
    if os.environ.get('LISTEN_PID')!=str(os.getpid()) or os.environ.get('LISTEN_FDS')!='1':raise SystemExit('Socket activation required.')
    s=Server('',Handler,bind_and_activate=False);s.socket.close();s.socket=socket.socket(fileno=3)
    s.store=store;s.web_uid=pwd.getpwnam('awb-web').pw_uid;s.read_uids={pwd.getpwnam(u).pw_uid for u in ('awb','awb-ask')}
    threading.Thread(target=store.worker,daemon=True).start();s.serve_forever()
if __name__=='__main__':main()
