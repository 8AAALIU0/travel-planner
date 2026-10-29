"""高德步骤、轨迹与静态底图。使用原有Web服务Key，凭据不发往浏览器。"""
import base64
import json
import math
from urllib.request import Request,ProxyHandler,build_opener
from urllib.parse import urlencode
from urllib.error import HTTPError,URLError
from .amap_client import AmapError,text_value
from .planner_api import cache_key,read_cache,write_cache


def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and n>=0 else None
    except (ValueError,TypeError):return None


def points(value):
    if not isinstance(value,str):return []
    out=[]
    for pair in value.split(';')[:10000]:
        try:
            a,b=map(float,pair.split(','))
            if math.isfinite(a) and math.isfinite(b) and -180<=a<=180 and -85<=b<=85:out.append([round(a,6),round(b,6)])
        except (ValueError,TypeError):pass
    return out


def simplify(path,maximum=90):
    if len(path)<=maximum:return path
    return [path[round(i*(len(path)-1)/(maximum-1))] for i in range(maximum)]


def parse_details(selected,mode):
    instructions=[];trace=[]
    def add(item,kind='步行'):
        if not isinstance(item,dict):return
        line=points(item.get('polyline'))
        trace.extend(line)
        text=text_value(item.get('instruction'),400)
        if text:
            instructions.append({'kind':kind,'text':text,'metres':number(item.get('distance')),'seconds':number(item.get('duration'))})
    if mode=='transit':
        for segment in selected.get('segments',[]) if isinstance(selected.get('segments'),list) else []:
            if not isinstance(segment,dict):continue
            walking=segment.get('walking')
            if isinstance(walking,dict):
                for step in walking.get('steps',[]) if isinstance(walking.get('steps'),list) else []:add(step)
            bus=segment.get('bus')
            lines=bus.get('buslines') if isinstance(bus,dict) else None
            if isinstance(lines,list) and lines and isinstance(lines[0],dict):
                line=lines[0];trace.extend(points(line.get('polyline')))
                dep=line.get('departure_stop') or {};arr=line.get('arrival_stop') or {}
                dep=dep if isinstance(dep,dict) else {};arr=arr if isinstance(arr,dict) else {}
                name=text_value(line.get('name'),150)
                text=f"在{text_value(dep.get('name'),80) or '未返回上车站'}上车，乘坐{name or '未返回线路名'}，在{text_value(arr.get('name'),80) or '未返回下车站'}下车"
                if line.get('via_num') is not None:text+=f"，途经{line['via_num']}站"
                instructions.append({'kind':text_value(line.get('type'),30) or '公交/地铁','text':text,'metres':number(line.get('distance')),'seconds':number(line.get('duration'))})
            railway=segment.get('railway')
            if isinstance(railway,dict) and railway.get('name'):
                instructions.append({'kind':'铁路','text':text_value(railway.get('name'),200),'metres':number(railway.get('distance')),'seconds':number(railway.get('time'))})
    else:
        for step in selected.get('steps',[]) if isinstance(selected.get('steps'),list) else []:add(step,'驾车' if mode=='driving' else '步行')
    dedup=[]
    for p in trace:
        if not dedup or dedup[-1]!=p:dedup.append(p)
    fare=number(selected.get('cost')) if mode=='transit' else None
    return {'instructions':instructions[:150],'polyline':simplify(dedup,800),'fare':fare,'detail_version':8}


def static_params(leg,start,end):
    line=simplify(leg.get('polyline') or [],90)
    if len(line)<2:raise AmapError('这段高德路线没有返回道路轨迹，无法绘制真实路线图。可打开高德导航核对。')
    text=';'.join(f'{p[0]:.6f},{p[1]:.6f}' for p in line)
    markers=f"mid,0x237765,起:{start['longitude']:.6f},{start['latitude']:.6f}|mid,0xe67b4a,终:{end['longitude']:.6f},{end['latitude']:.6f}"
    return {'size':'750*420','scale':'1','markers':markers,'paths':'6,0x237765,1,,:'+text}


def static_map(key,leg,start,end):
    if not key:raise AmapError('本次启动没有高德Web服务Key，地图暂不可用。')
    params=static_params(leg,start,end)
    identifier=cache_key('amap-static-v8',params)
    cached=read_cache(identifier)
    if cached:
        return base64.b64decode(cached['image']),cached['mime']
    params.update(key=key)
    opener=build_opener(ProxyHandler({}))
    try:
        with opener.open(Request('https://restapi.amap.com/v3/staticmap?'+urlencode(params)),timeout=15) as r:
            body=r.read(3000001)
    except HTTPError as exc:
        raise AmapError(f'高德静态地图HTTP {exc.code}，请核对该接口权限。') from None
    except (URLError,OSError,TimeoutError):
        raise AmapError('高德底图连接失败或超时，文字路线仍可查看。') from None
    if len(body)>3000000:raise AmapError('高德地图图片超过大小限制。')
    if body.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
    elif body.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
    else:
        try:
            data=json.loads(body.decode('utf-8'))
            code=text_value(data.get('infocode'),40);info=text_value(data.get('info'),150).replace(key,'[隐藏]')
        except (ValueError,UnicodeError,AttributeError):code,info='','返回内容不是地图图片'
        raise AmapError(f'高德静态地图 {code}：{info}。请核对该Key的静态地图权限或额度；详细导航仍可查看。')
    write_cache(identifier,{'image':base64.b64encode(body).decode(),'mime':mime},hours=1)
    return body,mime
