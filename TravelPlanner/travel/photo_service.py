"""照片展示专用：固定提供商、名称/坐标核对、小图本地缓存；不参与旅游报价。"""
import base64
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import socket
import threading
import unicodedata
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from django.utils import timezone
from .models import PlannerCache

UA='TravelPlannerCourseProject/1.1 (local educational travel photo viewer)'
_lock=threading.BoundedSemaphore(2)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def _fetch(url,limit=2_000_000):
    """Only fixed API/image providers; never follow arbitrary redirects."""
    p=urlsplit(url);host=(p.hostname or '').lower()
    allowed=host in ('restapi.amap.com','www.wikidata.org','commons.wikimedia.org','upload.wikimedia.org','thumb.wikimedia.org') or host.endswith(('.autonavi.com','.amap.com','.aoscdn.com'))
    if p.scheme not in ('https','http') or not allowed or p.username or p.password or p.port not in (None,80,443):raise ValueError('unsupported photo host')
    addresses=socket.getaddrinfo(host,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('nonpublic host')
    with build_opener(NoRedirect()).open(Request(url,headers={'User-Agent':UA,'Accept':'application/json,image/*;q=0.9'}),timeout=4) as response:
        raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('photo response too big')
        return raw


def _json(base,**params):
    data=json.loads(_fetch(base+'?'+urlencode(params)).decode('utf-8-sig'))
    if not isinstance(data,dict):raise ValueError('invalid response')
    return data


def _key(prefix,value):return hashlib.sha256((prefix+str(value)).encode()).hexdigest()

def _cached(key):
    row=PlannerCache.objects.filter(cache_key=key,expires_at__gt=timezone.now()).first()
    return row.payload if row else None

def _save(key,payload,hours):
    PlannerCache.objects.update_or_create(cache_key=key,defaults={'payload':payload,'expires_at':timezone.now()+timedelta(hours=hours)})

def _picture(url,source,**credits):
    try:
        raw=_fetch(url)
        if raw.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
        elif raw.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
        elif raw.startswith(b'RIFF') and raw[8:12]==b'WEBP':mime='image/webp'
        else:return None
        return dict(data=base64.b64encode(raw).decode(),mime=mime,source=source,**credits)
    except Exception:return None


def _normal(text):
    text=unicodedata.normalize('NFKC',str(text)).lower()
    return re.sub(r'[\s·•・—_\-()（）]+','',text)


def _names(name):
    # Only strip public entrance suffixes, not the words that distinguish landmarks.
    clean=re.sub(r'[-—（(]?(?:东|西|南|北)?(?:门|入口|售票处|游客中心)[）)]?$','',str(name)).strip('-— ')
    short=re.sub(r'(?:风景名胜区|风景区|旅游景区|景区|公园)$','',clean)
    return {v for v in (_normal(name),_normal(clean),_normal(short)) if len(v)>=2}


def _distance(a,b):
    lon1,lat1,lon2,lat2=map(math.radians,[float(a[0]),float(a[1]),float(b[0]),float(b[1])])
    v=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371*2*math.asin(min(1,math.sqrt(v)))


def _verified(entity,stop):
    names=_names(stop.get('name',''))
    labels=[x.get('value','') for x in entity.get('labels',{}).values()]
    labels += [x.get('value','') for xs in entity.get('aliases',{}).values() for x in xs]
    if not names.intersection({_normal(x) for x in labels}):return False
    try:
        origin=(stop['longitude'],stop['latitude'])
        if origin[0] is None or origin[1] is None:return False
        coords=entity.get('claims',{}).get('P625',[])
        for claim in coords:
            val=claim.get('mainsnak',{}).get('datavalue',{}).get('value',{})
            if val.get('globe','http://www.wikidata.org/entity/Q2').endswith('/Q2') and _distance(origin,(val['longitude'],val['latitude']))<=5:return True
    except (KeyError,ValueError,TypeError):pass
    return False


def _plain(value):return html.unescape(re.sub('<[^>]*>','',str(value or '')))[:400]


def _commons(stop):
    provider_key=_key('photo-provider:','wikimedia')
    if _cached(provider_key):return None
    try:
        search=_json('https://www.wikidata.org/w/api.php',action='wbsearchentities',search=stop['name'],language='zh',uselang='zh',limit=3,format='json')
        ids=[r['id'] for r in search.get('search',[]) if re.fullmatch(r'Q\d+',r.get('id',''))][:3]
        if not ids:return None
        data=_json('https://www.wikidata.org/w/api.php',action='wbgetentities',ids='|'.join(ids),props='labels|aliases|claims',languages='zh|zh-hans|zh-hant|en',format='json')
        for entity in data.get('entities',{}).values():
            if not _verified(entity,stop):continue
            for claim in entity.get('claims',{}).get('P18',[])[:2]:
                filename=claim.get('mainsnak',{}).get('datavalue',{}).get('value')
                if not isinstance(filename,str):continue
                info=_json('https://commons.wikimedia.org/w/api.php',action='query',prop='imageinfo',titles='File:'+filename,iiprop='url|extmetadata',iiurlwidth=800,format='json')
                for page in info.get('query',{}).get('pages',{}).values():
                    for image in page.get('imageinfo',[])[:1]:
                        meta=image.get('extmetadata',{})
                        def m(k):return _plain(meta.get(k,{}).get('value',''))
                        license=m('LicenseShortName');license_url=m('LicenseUrl')
                        if not license or m('NonFree').lower()=='true':continue
                        if not (license.startswith(('CC BY','CC0','Public domain','PDM'))):continue
                        url=image.get('thumburl') or image.get('url','')
                        if (urlsplit(url).hostname or '') not in ('upload.wikimedia.org','thumb.wikimedia.org'):continue
                        result=_picture(url,'Wikimedia Commons',page_url=image.get('descriptionurl',''),author=m('Artist') or m('Attribution'),license=license,license_url=license_url,note='公开图库 · 名称及附近坐标匹配；画面按卡片裁切。')
                        if result:return result
    except Exception:
        # Inaccessible public provider won't be retried separately for every card.
        _save(provider_key,{'unavailable':True},1/6)
    return None


def _amap(stop):
    candidates=[]
    for p in stop.get('photos',[]) if isinstance(stop.get('photos'),list) else []:
        u=p.get('url','') if isinstance(p,dict) else p
        if isinstance(u,str):candidates.append(u)
    for url in candidates[:2]:
        result=_picture(url,'高德地点照片',note='对应高德POI提供的地点照片')
        if result:return result
    api=os.environ.get('TRAVEL_AMAP_KEY','')
    poi=stop.get('poi_id','')
    if not api or not re.fullmatch('[A-Za-z0-9_]{3,64}',poi):return None
    try:
        data=_json('https://restapi.amap.com/v5/place/detail',key=api,id=poi,show_fields='photos',output='json')
        if str(data.get('status'))!='1':return None
        for row in data.get('pois',[])[:1]:
            if row.get('id')!=poi:continue
            for photo in row.get('photos',[])[:2]:
                url=photo.get('url','') if isinstance(photo,dict) else ''
                if url in candidates:continue
                result=_picture(url,'高德地点详情',note='补查对应高德POI详情取得的照片')
                if result:return result
    except Exception:pass
    return None


def photo_key(stop):
    return _key('scenic-photo-v16:',json.dumps([stop.get(k) for k in ('poi_id','name','longitude','latitude')],ensure_ascii=False))


def resolve_photo(stop):
    key=photo_key(stop)
    cached=_cached(key)
    if cached is not None:return cached
    if not _lock.acquire(blocking=False):return {'retry':True}
    try:
        result=_amap(stop) or _commons(stop) or {'missing':True,'message':'尚未取得匹配照片，可选择本机照片或查看网络图片。'}
        result['photo_cache_v16']=True
        _save(key,result,24*30 if result.get('data') else 2)
        # Bound the number of persistent photo rows (including expired entries).
        keys=list(PlannerCache.objects.filter(payload__photo_cache_v16=True).order_by('-expires_at').values_list('pk',flat=True)[80:])
        if keys:PlannerCache.objects.filter(pk__in=keys).delete()
        return result
    finally:_lock.release()
