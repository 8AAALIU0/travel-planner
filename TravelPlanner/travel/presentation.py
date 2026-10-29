"""页面展示层：照片与简述；不改变查询、票价和行程选择。"""
import copy
import re
from urllib.parse import quote
from .photo_service import photo_key
from urllib.parse import urlsplit
from .models import AmapRecord,Attraction


def image_urls(values):
    result=[]
    for value in values if isinstance(values,list) else []:
        if isinstance(value,dict):value=value.get('url','')
        if not isinstance(value,str):continue
        try:
            u=urlsplit(value)
            valid=u.scheme in ('http','https') and u.hostname and not u.username and not u.password and len(value)<=2000
            if not valid or u.hostname.lower() in ('localhost','127.0.0.1','::1'):continue
        except ValueError:continue
        if value not in result:result.append(value)
    return result[:6]


def brief(row,description=''):
    description=re.sub(r'\s+',' ',str(description or '')).strip()
    if description:return description[:360],'已维护的景点资料'
    text=row.get('category','')+row['name']
    if any(w in text for w in ('博物馆','美术馆','纪念馆')):
        content='把一段旅途留给展馆，在展陈与作品之间慢慢看。适合对历史、文化或艺术感兴趣的旅行者；具体展览及预约要求请以场馆公布为准。'
    elif any(w in text for w in ('游乐园','乐园','游乐场','欢乐谷')):
        content='为行程增加一点轻松与活力，适合喜欢游乐体验的旅行者。可参与项目、身高年龄限制与开放安排，需要以乐园当天公布的信息为准。'
    elif any(w in text for w in ('动物园','水族馆','海洋馆','野生动物')):
        content='将观察动物或海洋生物的体验放进旅程，适合对自然感兴趣的旅行者。展区、表演和互动项目以场馆当天安排为准。'
    elif any(w in text for w in ('公园','湖','湿地','森林','山岳')):
        content='在行程里留一段慢下来的时间，安排散步、观景或短暂休息。具体游览范围、步行强度和现场开放情况，请在出发前核对。'
    elif any(w in text for w in ('古迹','遗址','寺','故居','文化')):
        content='用一段停留感受目的地的文化氛围。可以结合现场介绍与导览慢慢参观；历史细节、开放区域和参观要求以景区资料为准。'
    else:content='这是本次兴趣检索与行程排程中的一处候选地点。可以结合所在位置、预计停留时间与相邻景点，选择适合自己的游览节奏。'
    area=str(row.get('city',''))+str(row.get('district',''))
    return (('地点位于'+area+'。') if area else '')+content,'按地点类别整理的简述'


def present_job(source):
    if not source:return None
    job=copy.deepcopy(source)
    plan=job.get('plan') or {}
    stops=[s for d in plan.get('days',[]) for s in d.get('stops',[])]
    ids=[s['poi_id'] for s in stops if s.get('poi_id')]
    records={r.poi_id:r for r in AmapRecord.objects.select_related('attraction').filter(poi_id__in=ids)} if ids else {}
    pks=[s['detail_pk'] for s in stops if s.get('detail_pk')]
    descriptions=dict(Attraction.objects.filter(pk__in=pks).values_list('pk','description')) if pks else {}
    for s in stops:
        record=records.get(s.get('poi_id'))
        s['ui_photo_key']=photo_key(s)
        s['ui_image_search']='https://image.baidu.com/search/index?tn=baiduimage&word='+quote(str(s.get('city') or plan.get('city',''))+' '+s.get('name','')+' 风景')
        s['ui_photos']=image_urls(s.get('photos',[])) or (image_urls(record.photos) if record else [])
        description=descriptions.get(s.get('detail_pk')) or (record.attraction.description if record else '')
        s['ui_intro'],s['ui_intro_source']=brief(s,description)
    for hotel in job.get('hotels',[]):
        photos=image_urls([hotel.get('raw',{}).get('firstPic','')])
        hotel['ui_photo']=photos[0] if photos else ''
    return job
