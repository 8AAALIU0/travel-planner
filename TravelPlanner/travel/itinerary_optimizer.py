"""多日、预算及时间窗约束的有界束搜索。比较多个组合，不声称全局最优。"""
from dataclasses import dataclass,replace
from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .amap_client import AmapError
from .planner_api import cache_key,read_cache,write_cache
from .planner_service import day_window,monday_closed,opening_window,estimate_leg,time_text,route_link


@dataclass(frozen=True)
class State:
    used:int=0
    themes:int=0
    cost:int=0
    utility:float=0
    travel:int=0
    days:tuple=()
    current:tuple=()
    clock:int=0
    lunch:bool=False


def cents(amount):
    return int((Decimal(str(amount))*100).quantize(Decimal('1')))


def ticket_for_day(source,p,day):
    row=dict(source)
    if p.get('ticket_policy'):
        from .ticket_budgeting import apply
        return apply(row,p,day)
    ref=next((r for r in p.get('ticket_refs',[]) if r.get('name')==row['name'] and r.get('date')==day.isoformat()),None)
    if ref:
        stamp=parse_datetime(ref.get('queried_at',''))
        if stamp and timezone.is_aware(stamp) and 0<=(timezone.now()-stamp).total_seconds()<=86400:
            try:
                amount=Decimal(ref['unit'])
                if amount.is_finite() and amount>=0:
                    row.update(ticket_per_person=str(max(amount,Decimal(row['ticket_per_person']))),price_reference=False,tuniu_reference=True,ticket_source_url='',
                        ticket_type=('接口成人票起价参考：' if ref.get('auto') else '用户选定票种参考：')+ref['label']+'；起价日期 '+ref.get('priceday','未知')+'。仍为预算预留，非当天实价。')
            except (ValueError,ArithmeticError,KeyError):pass
    return row


def edge_key(a,b):return (a['poi_id'],b['poi_id'])


def rank(s,min_stops=0,min_days=0):
    count=sum(len(d) for d in s.days)+len(s.current)
    play_days=sum(bool(d) for d in s.days)+bool(s.current)
    return (min(play_days,min_days),min(count,min_stops),s.utility,count,-s.travel,-s.cost)


def prune(states,width,min_stops=0,min_days=0):
    # 同一已选集合、末点、当天长度与结束时刻，保留目标分更高的状态。
    best={}
    for s in states:
        last=s.current[-1]['poi_id'] if s.current else ''
        key=(s.used,last,len(s.current),s.clock,s.cost,s.lunch,sum(bool(d) for d in s.days) if min_days else 0)
        if key not in best or rank(s,min_stops,min_days)>rank(best[key],min_stops,min_days):best[key]=s
    return sorted(best.values(),key=lambda s:rank(s,min_stops,min_days),reverse=True)[:width]


def search(candidates,p,ticket_budget,known=None,errors=None,closed=False,width=48):
    known=known or {};errors=errors or {}
    cap=2 if p['pace']=='relaxed' else 3
    limit=cents(ticket_budget)
    n=len(candidates)
    estimates={(a['poi_id'],b['poi_id']):estimate_leg(a,b,p['mode']) for a in candidates for b in candidates if a['poi_id']!=b['poi_id']}
    min_stops=p.get("_minimum_sights",0);min_days=p.get("_minimum_play_days",0)
    states=[State()]
    metrics=[]
    for index in range(p['days']):
        day=p['start_date']+timedelta(days=index)
        start,end=day_window(p,index)
        prepared=[ticket_for_day(r,p,day) for r in candidates]
        active=[replace(s,current=(),clock=start,lunch=start>=13*60) for s in states]
        completed=[]
        rejected={'time':0,'budget':0,'travel':0,'closed':0}
        for slot in range(cap):
            completed.extend(active)
            expanded=[]
            for s in active:
                for i,row in enumerate(prepared):
                    if s.used & (1<<i):continue
                    if monday_closed(row,day):rejected['closed']+=1;continue
                    amount=cents(Decimal(row['ticket_per_person'])*p['people'])
                    if s.cost+amount>limit:rejected['budget']+=1;continue
                    leg=None
                    if s.current:
                        pair=edge_key(s.current[-1],row)
                        if closed and pair not in known and pair not in errors:
                            rejected['travel']+=1;continue
                        leg=dict(known.get(pair) or estimates[pair])
                        if pair in errors:leg['error']=errors[pair]
                        if leg['minutes']>(60 if p['mode']=='walking' else 90):rejected['travel']+=1;continue
                    begin=s.clock+(leg['minutes'] if leg else 0)
                    window=opening_window(row)
                    if window:begin=max(begin,window[0])
                    lunch=s.lunch
                    # 下午抵达后不再机械增加午休；只在跨越午间且尚未休息时预留。
                    lunch_before=bool(s.current and not lunch and begin<13*60 and begin+row['visit_minutes']>12*60)
                    if lunch_before:begin+=45;lunch=True
                    finish=begin+row['visit_minutes']
                    if finish>end or (window and finish>window[1]):rejected['time']+=1;continue
                    travel=leg['minutes'] if leg else 0
                    # 可行景点数与兴趣收益为主要目标，通勤、等候与费用为次要惩罚。
                    gain=100+5*min(30,max(0,float(row.get('score',0))))-.55*travel-.025*max(0,begin-s.clock-travel)
                    gain+=8 if not s.current else 0
                    gain+=2*(p['days']-index)
                    theme_bits=sum(1<<i for i,w in enumerate(p.get('keywords',[])) if w in row.get('keyword_matches',[]))
                    gain+=18*(theme_bits & ~s.themes).bit_count()
                    stop=dict(row,begin=time_text(begin),end=time_text(finish),leg=leg,group_ticket=str(Decimal(amount)/100),
                              hours_checked=bool(window),lunch_before=lunch_before,
                              map_link=route_link(s.current[-1],row,p['mode']) if s.current else '')
                    expanded.append(replace(s,used=s.used|(1<<i),themes=s.themes|theme_bits,cost=s.cost+amount,utility=s.utility+gain,
                        travel=s.travel+travel,current=s.current+(stop,),clock=finish,lunch=lunch))
            if not expanded:active=[];break
            active=prune(expanded,width,min_stops,min_days)
        completed.extend(active)
        states=prune([replace(s,days=s.days+(s.current,),current=(),clock=0,lunch=False) for s in completed],width,min_stops,min_days)
        metrics.append(rejected)
    return max(states,key=lambda s:rank(s,min_stops,min_days)),metrics


def optimize(candidates,p,ticket_budget,key,route_function,candidate_limit=24,width=48,request_limit=20):
    selected=candidates[:candidate_limit]
    known={};errors={};queried=set();used_requests=0
    state=None;metrics=[];rounds=0;was_slow=False
    def resolve(items):
        nonlocal used_requests,was_slow
        def fetch(item):
            pair,a,b,identifier=item
            try:return item,route_function(key,a,b,p['mode'],p['city'],use_cache=False),''
            except AmapError as exc:return item,None,str(exc)
        with ThreadPoolExecutor(max_workers=3) as pool:responses=list(pool.map(fetch,items))
        for (pair,a,b,identifier),leg,error in responses:
            used_requests+=1;queried.add(pair)
            if leg:
                known[pair]=leg;write_cache(identifier,leg,hours=1)
                if leg['minutes']>(60 if p['mode']=='walking' else 90):was_slow=True
            else:errors[pair]=error
    # 先核验少量高分景点的近邻边，避免粗估偏大导致可行的半日组合从未被查询。
    probes=[];seen=set()
    for a in selected[:4]:
        nearby=sorted((b for b in selected if b['poi_id']!=a['poi_id']),key=lambda b:estimate_leg(a,b,p['mode'])['km'])[:2]
        for b in nearby:
            pair=edge_key(a,b)
            if pair in seen:continue
            seen.add(pair)
            identifier=cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
            cached=read_cache(identifier)
            if cached:known[pair]=cached
            elif len(probes)<min(8,request_limit//2):probes.append((pair,a,b,identifier))
    if probes:resolve(probes)
    # 重算后补核验新选中的边；达到上限后只在已核验或已明确失败的边中重排。
    for _ in range(5):
        rounds+=1
        state,metrics=search(selected,p,ticket_budget,known,errors,closed=used_requests>=request_limit,width=width)
        pairs={edge_key(a,b):(a,b) for path in state.days for a,b in zip(path,path[1:])}
        pending=[]
        for pair,(a,b) in pairs.items():
            if pair in known or pair in errors:continue
            identifier=cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
            cached=read_cache(identifier)
            if cached:known[pair]=cached
            elif used_requests+len(pending)<request_limit:pending.append((pair,a,b,identifier))
        if not pending:
            # 缓存也可能改变时长，因此再用真实时长排一次，然后终检。
            if not any(k in pairs for k in known):break
            refreshed,_=search(selected,p,ticket_budget,known,errors,closed=used_requests>=request_limit,width=width)
            new_pairs={edge_key(a,b) for path in refreshed.days for a,b in zip(path,path[1:])}
            if all(k in known or k in errors for k in new_pairs):state=refreshed;break
            if used_requests>=request_limit:state=refreshed;break
            state=refreshed
            continue
        resolve(pending)
    # 最终只用已有核验结果/明确失败的边，避免把尚未查询的新边标成已核验。
    state,metrics=search(selected,p,ticket_budget,known,errors,closed=True,width=width)
    days=[];verified=unverified=0
    for index,path in enumerate(state.days):
        start,end=day_window(p,index)
        stops=list(path)
        for stop in stops:
            leg=stop.get('leg')
            if leg:
                if leg.get('verified'):verified+=1
                else:unverified+=1
        date=p['start_date']+timedelta(days=index)
        if start>=end:reason='本日处于去程在途或返程窗口内，没有足够的景点游玩时间。'
        elif len(stops)<(2 if p['pace']=='relaxed' else 3):
            reason='在可用时段、预算、开放时间及通勤限制内选择了当前组合；每日数量为上限，不强行凑满。'
        else:reason='已达到本日所选游玩节奏的景点数量上限。'
        days.append({'number':index+1,'date':date.isoformat(),'stops':stops,'available_start':time_text(start) if start<1440 else '尚未抵达',
                     'available_end':time_text(max(0,end)),'available_text':('在途或返程预留，本日没有可用游玩时段' if start>=end else time_text(start)+'—'+time_text(end)),'available_minutes':max(0,end-start),'schedule_reason':reason,
                     'target':2 if p['pace']=='relaxed' else 3,'travel_minutes':sum(s['leg']['minutes'] for s in stops if s.get('leg'))})
    return {'days':days,'verified_legs':verified,'unverified_legs':unverified,'slow_repaired':was_slow,
            'optimizer':{'method':'预算与时间窗约束下的多日束搜索＋真实路线核验后重排','beam_width':width,
             'considered_candidates':len(selected),'available_candidates':len(candidates),'route_requests':used_requests,'rounds':rounds,
             'objective_score':round(state.utility,2),'guarantee':'有界候选与搜索范围内的可行方案，不保证全局最优'}}
