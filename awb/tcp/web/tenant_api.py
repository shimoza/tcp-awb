"""Bounded read-only tenant inventory through the existing key-service socket."""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from awb import config
from awb.tcp import keys, tenants, sweep

SOURCES = (
 ('ecs','ecs','/v1/{project_id}/cloudservers/detail','servers','offset',{}),
 ('evs','evs','/v2/{project_id}/cloudvolumes/detail','volumes','offset',{}),
 ('eip','vpc','/v1/{project_id}/publicips','publicips','marker',{}),
 ('vpc','vpc','/v1/{project_id}/vpcs','vpcs','marker',{}),
 ('ims','ims','/v2/cloudimages','images','marker',{'__imagetype':'private'}),
)
CACHE = Path('/var/lib/awb-console-data/tenants.json')
TTL = 300
MIN_REFRESH = 30

class InventoryError(Exception):
    pass


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def call(alias,region,service,path,query=None):
    try:
        reply = keys.request(keys.call_socket(), {'op':'call','tenant':alias,'role':'read','method':'GET',
                   'service':service,'path':path,'region':region,'query':query or {}}, timeout=70)
    except (keys.KeysError,OSError):
        raise InventoryError('The read connection is unavailable.') from None
    if not reply.get('ok') or not 200 <= (reply.get('status') or 0) < 300:
        status=reply.get('status')
        raise InventoryError('Read access unavailable'+(f' (HTTP {status}).' if isinstance(status,int) and status else '.'))
    if not isinstance(reply.get('data'),dict):
        raise InventoryError('The service returned no readable inventory.')
    return reply['data']


def listing(alias,region,source,caller=call):
    kind,service,path,key,paging,extra=source
    query={**extra,'limit':'100'};result=[];seen=set()
    for page_number in range(50):
        data=caller(alias,region,service,path,query)
        page=data.get(key)
        if not isinstance(page,list) or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) for row in page):
            raise InventoryError('The service returned an incomplete inventory.')
        ids={row['id'] for row in page}
        if len(ids)!=len(page) or ids & seen:
            raise InventoryError('Inventory pagination repeated a resource.')
        result.extend(page);seen.update(ids)
        if len(page)<100:
            total=data.get('count')
            if isinstance(total,int) and total>len(result):
                raise InventoryError('The inventory is incomplete.')
            return result
        query=dict(query)
        query['offset' if paging=='offset' else 'marker']=str(len(result)) if paging=='offset' else page[-1]['id']
    raise InventoryError('The inventory exceeds the supported page limit.')


def safe_value(value,pattern,default=''):
    value=str(value or '')
    return value if re.fullmatch(pattern,value) else default


def project_tag(raw):
    tags=sweep.tags(raw.get('tags') or (raw.get('metadata') or {}).get('tags'))
    return safe_value(tags.get('awb-project'),r'tcp-[a-z0-9]{4}')


def project_from_name(raw):
    # Only an explicit AWB code is retained. Other resource names are not returned.
    matches=re.findall(r'(?<![a-z0-9])tcp-[a-z0-9]{4}(?![a-z0-9])',str(raw.get('name') or ''))
    return matches[0] if len(set(matches))==1 else ''


def positive_number(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=10**9 else None


def item(alias,region,kind,raw):
    handle=kind+'-'+hashlib.sha256((alias+'\0'+region+'\0'+raw['id']).encode()).hexdigest()[:8]
    state=safe_value(raw.get('status'),r'[A-Za-z0-9 _-]{1,40}','Unknown')
    size='';amount=None
    if kind=='ecs':
        flavor=raw.get('flavor') or {}
        size=safe_value(flavor.get('id') or flavor.get('name'),r'[a-zA-Z0-9._-]{1,70}') if isinstance(flavor,dict) else ''
    elif kind=='evs':
        amount=positive_number(raw.get('size'));size=f'{amount:g} GiB' if amount is not None else ''
    elif kind=='eip':
        amount=positive_number(raw.get('bandwidth_size'));size=f'{amount:g} Mbit/s' if amount is not None else ''
    elif kind=='ims':
        amount=positive_number(raw.get('min_disk'));size=f'{amount:g} GiB minimum disk' if amount is not None else ''
    tags=sweep.tags(raw.get('tags') or (raw.get('metadata') or {}).get('tags'))
    project=project_tag(raw);name_project=project_from_name(raw)
    created=raw.get('created') or raw.get('created_at') or ''
    try:created=dt.datetime.fromisoformat(created.replace('Z','+00:00')).isoformat(timespec='seconds')
    except (ValueError,TypeError,AttributeError):created=None
    return {'handle':handle,'kind':kind,'region':region,'state':state,'size':size,'amount':amount,
            'project':project or name_project,'project_source':'tag' if project else 'name' if name_project else None,
            'expiry':safe_value(tags.get('awb-expiry'),r'\d{4}-\d{2}-\d{2}'),'created':created}


def collect_tenant(tenant,caller=call):
    result={'alias':tenant.alias,'regions':list(tenant.regions),'domain_name':None,'domain_status':'unavailable',
            'scope':'accessible','scope_label':'All resources visible to the connected account',
            'ownership_verified':False,'collected_at':None,'coverage':[],'resources':[],'errors':[]}
    try:
        # Full IAM domain names were explicitly requested by the workspace owner.
        # Do not infer them from aliases, IDs or API hostnames.
        data=caller(tenant.alias,tenant.regions[0],'iam','/v3/auth/domains')
        domains=data.get('domains')
        if not isinstance(domains,list) or len(domains)!=1:
            raise InventoryError('A single IAM domain could not be identified.')
        name=domains[0].get('name','')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}',name):
            raise InventoryError('The IAM domain name could not be displayed.')
        result.update(domain_name=name,domain_status='available')
    except (InventoryError,IndexError):
        result['errors'].append('The full IAM domain name is unavailable.')
    for region in tenant.regions:
        for source in SOURCES:
            kind=source[0]
            try:
                rows=listing(tenant.alias,region,source,caller)
                result['resources'].extend(item(tenant.alias,region,kind,row) for row in rows)
                result['coverage'].append({'region':region,'kind':kind,'status':'complete','count':len(rows)})
            except InventoryError as e:
                result['coverage'].append({'region':region,'kind':kind,'status':'unavailable','count':None,'error':str(e)})
    result['collected_at']=stamp()
    resources=result['resources']
    def count(kind,predicate=lambda r:True):
        relevant=[c for c in result['coverage'] if c['kind']==kind]
        if not relevant or any(c['status']!='complete' for c in relevant):return None
        return sum(r['kind']==kind and predicate(r) for r in resources)
    result['counts']={s[0]:count(s[0]) for s in SOURCES}
    result['counts']['running']=count('ecs',lambda r:r['state'].upper()=='ACTIVE')
    result['counts']['stopped']=count('ecs',lambda r:r['state'].upper()=='SHUTOFF')
    result['counts']['attached_disks']=count('evs',lambda r:r['state'].lower()=='in-use')
    result['counts']['disk_gib']=sum(r['amount'] for r in resources if r['kind']=='evs') if result['counts']['evs'] is not None and all(r['amount'] is not None for r in resources if r['kind']=='evs') else None
    result['status']='complete' if all(c['status']=='complete' for c in result['coverage']) else 'partial'
    return result


class Inventory:
    def __init__(self,cache=CACHE,loader=None,collector=collect_tenant):
        self.cache=Path(cache);self.loader=loader or (lambda:tenants.load(config.paths()));self.collector=collector
        self.lock=threading.Lock();self.refreshing=False;self.last_attempt=0;self.error='';self.data=None
        try:
            saved=json.loads(self.cache.read_text())
            if isinstance(saved,dict) and isinstance(saved.get('tenants'),list):self.data=saved
        except (OSError,ValueError):pass
    def get(self,refresh=False):
        registered=self.loader();aliases={t.alias for t in registered}
        with self.lock:
            stale=not self.data or time.time()-self.data.get('timestamp',0)>TTL
            if not self.refreshing and (refresh or stale) and time.monotonic()-self.last_attempt>=MIN_REFRESH:
                self.refreshing=True;self.last_attempt=time.monotonic()
                threading.Thread(target=self.update,args=(registered,),daemon=True).start()
            data=dict(self.data or {'tenants':[],'collected_at':None})
            data['tenants']=[t for t in data['tenants'] if t['alias'] in aliases]
            data.update(refreshing=self.refreshing,stale=stale,error=self.error,registered_count=len(registered),refresh_interval_seconds=TTL,
                        scope_note='Counts cover resources visible to the connected account in the configured regions. They are not filtered by creator.',
                        coverage_note='Coverage: ECS servers, EVS disks, elastic IPs, VPCs and private images. Other services are not included.')
        return data
    def update(self,registered):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                rows=list(pool.map(self.collector,registered))
            data={'tenants':rows,'collected_at':stamp(),'timestamp':time.time()}
            self.cache.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.cache.with_suffix('.next');tmp.write_text(json.dumps(data)+'\n');os.chmod(tmp,0o600);os.replace(tmp,self.cache)
            with self.lock:self.data=data;self.error=''
        except Exception:
            with self.lock:self.error='The inventory could not be refreshed. The previous snapshot is shown when available.'
        finally:
            with self.lock:self.refreshing=False
