"""Read-only projection of registered AWB projects for the authenticated console."""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).

import argparse
import datetime
import json
import os
import re
import stat
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from awb import config, projects, review, vault, normalize
from awb import status as recorded

FILES = ('SCOPE.md', 'STATE.md', 'OPEN.md', 'RESOURCES.md')
LIMIT = 256 * 1024
CODE = re.compile(r'tcp-[a-z0-9]{4}')

class Unavailable(Exception):
    pass


def read_text(root, name):
    """Only bounded, direct regular files from a verified project directory."""
    if name not in FILES:
        raise ValueError('Unsupported file')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                return {'status': 'unavailable', 'text': '', 'modified': None}
            data = f.read(LIMIT+1)
            if len(data) > LIMIT:
                return {'status': 'unavailable', 'text': '', 'modified': None}
            return {'status': 'available', 'text': data.decode('utf-8'), 'modified': datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc).isoformat()}
    except FileNotFoundError:
        return {'status': 'missing', 'text': '', 'modified': None}
    except (OSError, UnicodeError):
        return {'status': 'unavailable', 'text': '', 'modified': None}
    finally:
        os.close(root_fd)


def checked(text, paths):
    """Use the check-only socket. Never read the vault or its register."""
    normal = normalize.normalize(text).text
    try:
        hits = vault.check_remote(normal, paths.check_socket)
        if not hits:
            return normal, False
        ranges = sorted((max(0,h['start']),min(len(normal),h['start']+h['length'])) for h in hits)
        merged = []
        for start,end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0],max(end,merged[-1][1]))
            else:
                merged.append((start,end))
        result = normal
        for start,end in reversed(merged):
            result = result[:start]+'[withheld]'+result[end:]
        if vault.check_remote(result,paths.check_socket):
            return '[Content withheld by the data check.]', True
        return result, True
    except (vault.VaultError, OSError):
        raise Unavailable('The data check is unavailable. Project content was not returned.') from None


def resource_rows(text):
    header=None;rows=[]
    for line in text.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells=[x.strip() for x in line.strip().strip('|').split('|')]
        if header is None:
            header=[x.lower() for x in cells]
            continue
        if all(set(c)<=set('-: ') for c in cells):
            continue
        rows.append(dict(zip(header,cells)))
    return rows


def folder_for(record, paths):
    root=Path(record.path)
    if root.name != record.code or root.parent != paths.projects_root or root.is_symlink():
        raise Unavailable('The registered project folder cannot be opened safely.')
    if not root.is_dir():
        raise Unavailable('The registered project folder is unavailable.')
    return root


def entries(paths):
    return [r for r in projects.load(paths) if r.platform=='tcp' and r.state!='deleted' and CODE.fullmatch(r.code)]


def base_record(record,paths):
    root=folder_for(record,paths)
    source={name:read_text(root,name) for name in FILES}
    scope=source['SCOPE.md']['text']
    goal=next((' '.join(line[7:].split()) for line in scope.splitlines() if line.startswith('- goal:')),'')
    goal,redacted=checked(goal,paths)
    open_source=source['OPEN.md']
    open_count=sum(line.startswith('- ') for line in open_source['text'].splitlines()) if open_source['status']=='available' else None
    resources=source['RESOURCES.md']
    live=sum(row.get('state','').lower() not in ('deleted','kept') for row in resource_rows(resources['text'])) if resources['status']=='available' else None
    dates=[f['modified'] for f in source.values() if f['modified']]
    result={'code':record.code,'kind':record.kind,'customer':record.customer,'state':record.state,'created':record.created,'goal':goal,'goal_redacted':redacted,'open_items':open_count,'live_resources':live,'updated':max(dates) if dates else None,'status':status_of(source['STATE.md'],root,paths)}
    return result,source,root


def status_of(state,root,paths):
    """The Status: and Next: lines of STATE.md after the data check, its date and the commits newer than it."""
    if state['status']!='available':
        return {'summary':None,'next':None,'updated':None,'behind':None,'redacted':False}
    lines=recorded.lines(state['text'])
    summary,red_summary=checked(lines.summary,paths) if lines.summary else (None,False)
    nxt,red_next=checked(lines.next,paths) if lines.next else (None,False)
    return {'summary':summary,'next':nxt,'updated':state['modified'],'behind':recorded.behind(root),'redacted':red_summary or red_next}


def project_list(paths):
    result=[]
    for record in entries(paths):
        try:row,_,_=base_record(record,paths)
        except Unavailable:
            raise
        result.append(row)
    result.sort(key=lambda r:r['updated'] or r['created'],reverse=True)
    return {'source':'AWB project register','fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'projects':result}


def review_rows(root):
    """The review states of the deliverables, or None when the folders cannot be read safely."""
    # The existing reviewer hashes deliverables. Refuse symlinks before calling it.
    for name in ('deliverables','reviews'):
        base=root/name
        if base.is_symlink():
            return None
        if not base.exists():
            continue
        count=0
        for folder,dirs,files in os.walk(base,followlinks=False):
            for leaf in dirs+files:
                count+=1
                f=Path(folder)/leaf
                if count>2000 or f.is_symlink():
                    return None
    try:
        return review.status(root)
    except (OSError,ValueError):
        return None


def safe_review_status(root,paths):
    found=review_rows(root)
    if found is None:
        return {'status':'unavailable','items':[]}
    try:
        items=[]
        for row in found[:300]:
            name,masked=checked(row['file'],paths)
            items.append({'file':name,'state':row['state'],'tier':row['tier'],'redacted':masked})
        return {'status':'available','items':items,'truncated':len(found)>300}
    except (OSError,ValueError):
        return {'status':'unavailable','items':[]}


BOARD_ITEMS = 5
"""The most waiting-on and open items the board shows for one project."""
WITHHELD = '[Content withheld by the data check.]'


def bullets(text,heading=None):
    """The dash items of a Markdown text, or only those under the ## heading named."""
    out,inside=[],heading is None
    for line in text.splitlines():
        if heading is not None and line.startswith('## '):
            inside=line[3:].strip().lower()==heading.lower()
            continue
        if inside and line.startswith('- '):
            out.append(' '.join(line[2:].split()))
    return out


def board(paths):
    """Every active project for management: the recorded status with its time and lag, what the project waits on,
    its first open items, its deliverables by review state and its live resources. Codes only; the texts of one
    project pass the data check in one call."""
    rows=[]
    for record in entries(paths):
        if record.state!='active':
            continue
        row,source,root=base_record(record,paths)
        state=source['STATE.md']['text'] if source['STATE.md']['status']=='available' else ''
        opened=source['OPEN.md']['text'] if source['OPEN.md']['status']=='available' else ''
        waiting,items=bullets(state,'Waiting on'),bullets(opened)
        texts=waiting[:BOARD_ITEMS]+items[:BOARD_ITEMS]
        redacted=False
        if texts:
            joined,redacted=checked('\n'.join(texts),paths)
            parts=joined.split('\n')
            texts=parts if len(parts)==len(texts) else [WITHHELD]*len(texts)
        found=review_rows(root)
        counts={}
        for r in found or []:
            counts[r['state']]=counts.get(r['state'],0)+1
        n=min(len(waiting),BOARD_ITEMS)
        rows.append(dict(row,waiting_on={'items':texts[:n],'total':len(waiting)},
                         open={'items':texts[n:],'total':len(items)},
                         deliverables={'status':'unavailable' if found is None else 'available','counts':counts},
                         board_redacted=redacted))
    rows.sort(key=lambda r:r['updated'] or r['created'],reverse=True)
    summary={'projects':len(rows),
             'current':sum(1 for r in rows if r['status']['summary'] and r['status']['behind']==0),
             'lagging':sum(1 for r in rows if r['status']['behind']),
             'without_status':sum(1 for r in rows if not r['status']['summary'])}
    return {'source':'AWB project register','generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'summary':summary,'projects':rows}


def project_detail(code,paths):
    if not CODE.fullmatch(code):
        return None
    record=next((r for r in entries(paths) if r.code==code),None)
    if record is None:
        return None
    result,source,root=base_record(record,paths)
    documents={}
    for name,f in source.items():
        text,redacted=checked(f['text'],paths) if f['status']=='available' else ('',False)
        documents[name]={'text':text,'status':f['status'],'redacted':redacted,'modified':f['modified']}
    result['documents']=documents
    result['deliverables']=safe_review_status(root,paths)
    result['fetched_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


class Handler(BaseHTTPRequestHandler):
    def reply(self,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path!='/api/board' and not re.fullmatch(r'/api/projects(?:/tcp-[a-z0-9]{4})?',path):
            self.reply(404,{'error':'No such project endpoint.'});return
        try:
            paths=config.paths()
            if vault.ping(paths.check_socket)=='locked':
                raise Unavailable('The data check is locked. Project content was not returned.')
            if path=='/api/projects':data=project_list(paths)
            elif path=='/api/board':data=board(paths)
            else:data=project_detail(path.rsplit('/',1)[1],paths)
            self.reply(200,data) if data is not None else self.reply(404,{'error':'This project is not registered or has been deleted.'})
        except Unavailable as e:self.reply(503,{'error':str(e)})
        except Exception:self.reply(503,{'error':'Project data could not be read. Try again shortly.'})
    do_HEAD=do_GET
    def do_POST(self):self.reply(405,{'error':'Projects are read only.'})
    do_PUT=do_PATCH=do_DELETE=do_POST
    def log_message(self,fmt,*args):
        print(self.command,str(args[1]) if len(args)>1 else '-',flush=True)


def main():
    # the tenants moved to their own service and user (awb.tcp.web.tenant_api, F2)
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8182);args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.serve_forever()

if __name__=='__main__':main()
