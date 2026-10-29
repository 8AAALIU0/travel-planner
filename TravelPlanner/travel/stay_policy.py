"""酒店价格筛选与有来源的通勤比较；不把便宜等同于偏远。"""
import copy
import re
from decimal import Decimal
from datetime import date,timedelta
from .cost_service import money,QuoteError
from .amap_client import AmapError
from .planner_api import search_places,route_between,nearby_metro,cache_key,read_cache,write_cache
from .collect_service import preview_rows,canonical_city

LABELS={'economy':'价格优先','balanced':'价格与通勤均衡','convenient':'交通优先'}


def apply_stay(p):
    q=copy.deepcopy(p)
    q.setdefault('stay_kind','hotel');q.setdefault('hotel_preference','balanced')
    q.setdefault('ticket_policy','auto')
    if q['stay_kind']=='free':q.update(hotel=Decimal(0),rooms=1,nights=0,hotel_total=Decimal(0))
    return q


def lodging_cost(p,nights=None):
    if p.get('stay_kind')=='free':return Decimal(0)
    if p.get('hotel_total') is not None:return Decimal(str(p['hotel_total']))
    n=nights if nights is not None else p.get('nights')
    if n is None:n=p['days']-1
    return Decimal(str(p['hotel']))*p['rooms']*n


def first_stop(plan):
    return next((s for d in plan.get('days',[]) for s in d.get('stops',[]) if s.get('longitude') is not None),None)


def locate(name,city,key):
    if not key:return None
    identifier=cache_key('stay-location-v12',[name,city]);cached=read_cache(identifier)
    if cached is not None:return cached or None
    rows=preview_rows(search_places(key,city,name))
    def clean(s):return re.sub(r'[\s()（）·-]','',s)
    # 同城且完整名称一致才自动绑定，避免同品牌不同分店。
    exact=[r for r in rows if canonical_city(r['city'])==canonical_city(city) and clean(r['name'])==clean(name)]
    result=exact[0] if len(exact)==1 else None
    write_cache(identifier,result or {},hours=72)
    return result


def assess(row,p,anchor,key):
    row=copy.deepcopy(row);row['commute_minutes']=None;row['metro_text']='附近地铁信息尚未核实'
    try:
        point=locate(row['name'],p['city'],key)
        if point:
            row['location']=point
            if anchor:
                leg=route_between(key,point,anchor,p['mode'],p['city'])
                if leg.get('verified'):
                    row.update(commute_minutes=leg['minutes'],commute_km=leg['km'],commute_summary=leg.get('summary',''),commute_anchor=anchor['name'])
            try:
                metro=nearby_metro(key,point)
                if metro:
                    station=metro[0];metres=money(station.get('distance'))
                    row['metro_text']=str(station.get('name','地铁站'))+(f' · 直线距离约{metres}米（不是步行距离）' if metres is not None else ' · 步行距离未取得')
                else:row['metro_text']='周边1.5公里搜索未返回地铁站；不代表没有公交'
            except AmapError:pass
    except AmapError as exc:row['location_note']=str(exc)
    return row


def search_args(p,lodging,anchor):
    cap=Decimal(str(p['hotel']));preference=p.get('hotel_preference') or 'balanced'
    args={'cityName':canonical_city(p['city']),'checkIn':lodging['checkIn'],'checkOut':(date.fromisoformat(lodging['checkIn'])+timedelta(days=1)).isoformat(),'adultNum':min(p['people'],2),'prices':f'0-{cap}'}
    area=p.get('stay_area','').strip()
    if area:args['poiName']=area
    elif preference=='convenient' and anchor:args['poiName']=anchor['name']
    return args


def find_hotels(p,lodging,anchor,key,query):
    cap=Decimal(str(p['hotel']));preference=p.get('hotel_preference') or 'balanced'
    args=search_args(p,lodging,anchor)
    records=[];errors=[];attempts=[]
    def fetch(params,label):
        try:
            page=query('hotel',params)
            records.append(page)
            attempts.append({'label':label,'args':params,'returned':len(page['rows'])})
            return page
        except QuoteError as exc:
            errors.append(str(exc));attempts.append({'label':label,'args':params,'error':str(exc)})
            return None
    record=fetch(args,'首晚按预算查询')
    if record:
        qid=record.get('data',{}).get('queryId')
        try:pages=int(record.get('pages') or 1)
        except (ValueError,TypeError):pages=1
        if qid and pages>1:fetch({'queryId':str(qid),'pageNum':2},'首晚预算查询第2页')
    def collect():
        unique={};missing=0
        for page in records:
            for source in page['rows']:
                value=money(source.get('unit'))
                if value is None:missing+=1;continue
                token=source['raw'].get('hotelId') or source['name']
                if token not in unique or value<money(unique[token]['unit']):unique[token]=copy.deepcopy(source)
        return list(unique.values()),missing
    all_rows,missing=collect()
    rows=[r for r in all_rows if money(r['unit'])<=cap]
    blocked=any(any(w in e for w in ('密钥','权限','额度','频率','429')) for e in errors)
    if not rows and not blocked:
        # 预算查询可能没有返回低价库存；去掉价格参数核对实际响应，不扩大用户预算。
        fallback={k:v for k,v in args.items() if k!='prices'}
        fetch(fallback,'首晚不带价格筛选核对（预算保持不变）')
        all_rows,missing=collect()
        rows=[r for r in all_rows if money(r['unit'])<=cap]
    eligible=len(rows)
    above=sorted((r for r in all_rows if money(r['unit'])>cap),key=lambda r:Decimal(r['unit']))
    if rows:reason='within_budget';status='已取得预算范围内的首晚酒店参考。'
    elif above:reason='above_budget';status='本次返回的可识别首晚起价均超过每晚预算；下方仅展示最接近预算的参考，不计作预算内推荐。'
    elif missing:reason='missing_prices';status='接口返回了酒店，但缺少可识别价格，尚不能判断是否符合预算。'
    elif errors:reason='api_error';status='酒店接口未完成查询，请查看下方具体提示。'
    else:reason='no_results';status='当前城市、首晚日期和位置条件下，有限查询未返回酒店；不代表当地没有酒店。'
    if not rows:rows=above[:3]
    for row in rows:
        row['over_budget']=money(row['unit'])>cap
        row['over_by']=str(max(Decimal(0),money(row['unit'])-cap))
        row['quote_checkin']=args['checkIn'];row['quote_checkout']=args['checkOut']
        row['quote_scope']='首晚参考，后续晚数价格和连续入住库存尚未核实'
    rows.sort(key=lambda r:Decimal(r['unit']))
    # 核验最多6家，限制高德调用。价格和地理位置是独立指标，不预设越贵越方便。
    assessed=[assess(r,p,anchor,key) for r in rows[:6]]
    if preference=='economy':assessed.sort(key=lambda r:(Decimal(r['unit']),r['commute_minutes'] if r['commute_minutes'] is not None else 9999))
    elif preference=='convenient':assessed.sort(key=lambda r:(r['commute_minutes'] is None,r['commute_minutes'] or 0,Decimal(r['unit'])))
    else:
        prices=[float(Decimal(r['unit'])) for r in assessed]
        times=[r['commute_minutes'] for r in assessed if r['commute_minutes'] is not None]
        def score(r):
            price=float(Decimal(r['unit']));time=r['commute_minutes']
            a=(price-min(prices))/(max(prices)-min(prices)) if prices and max(prices)>min(prices) else 0
            b=(time-min(times))/(max(times)-min(times)) if time is not None and times and max(times)>min(times) else (1 if time is None else 0)
            return .55*a+.45*b
        assessed.sort(key=lambda r:(score(r),Decimal(r['unit'])))
    for r in assessed:r['preference_label']='超出预算，仅供参考' if r['over_budget'] else LABELS[preference]
    return assessed[:4],{'args':args,'queried_at':records[0]['queried_at'] if records else '',
        'pages':len(attempts),'eligible':eligible,'assessed':len(assessed),'returned':sum(len(x['rows']) for x in records),
        'unique_priced':len(all_rows),'missing_prices':missing,'above_budget':len(above),'reason':reason,'status':status,'attempts':attempts,
        'preference':LABELS[preference],'anchor':anchor['name'] if anchor else '', 'errors':errors,
        'stay_checkin':lodging['checkIn'],'stay_checkout':lodging['checkOut'],
        'note':'为避免整段起价误比每晚预算，本次只查询首晚。首晚起价不保证后续每晚价格或连续入住库存；住宿总预算仍按所填每晚预算×房间数×晚数估算。通勤只到首个景点，地铁距离为直线距离。'}
