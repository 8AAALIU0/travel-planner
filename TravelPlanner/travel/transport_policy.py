"""根据经济/均衡/时间偏好选择往返组合。候选有限，不承诺全网最低价。"""
import copy
from datetime import datetime
from decimal import Decimal
from .cost_service import QuoteError,money,seat_count

PRIORITIES={'economy':'经济优先','balanced':'均衡选择','fast':'时间优先'}
SEATS={'二等座':'edz','一等座':'ydz','硬座':'yz','硬卧':'yw','软卧':'rw','软座':'rz','商务座':'swz','特等座':'tdz','高级软卧':'gjrw','无座':'wz','动卧':'dw','一等卧':'ydw','二等卧':'edw'}


def timestamp(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (ValueError,TypeError):return None


def prepare(record,people,leg,allow_overnight=True,max_hours=None,seat_preference='any'):
    result=[]
    for source in record['rows']:
        if not source.get('usable') or money(source.get('unit')) is None:continue
        raw=source['raw'];dep=timestamp(raw.get('departureTime'));arr=timestamp(raw.get('arrivalTime'))
        if not dep or not arr or dep.date().isoformat()!=record['args']['departureDate']:continue
        try:duration=(arr-dep).total_seconds()/60
        except TypeError:continue
        if not 0<duration<=72*60:continue
        night=arr.date()>dep.date()
        if not allow_overnight and night:continue
        if max_hours and duration>float(max_hours)*60:continue
        if record['kind']=='train':
            label=source['name'].split(' · ')[-1]
            if label=='无座':continue
            if seat_preference=='sleeper' and '卧' not in label:continue
            if seat_preference=='second' and label!='二等座':continue
            if seat_preference=='ordinary' and label not in ('硬座','软座','硬卧','软卧','二等卧','动卧'):continue
            count=seat_count((raw.get('seatAvailable') or {}).get(SEATS.get(label,'')+'Num'))
        else:count=seat_count(raw.get('remainingSeats'))
        if count is None or count<people:continue
        row=copy.deepcopy(source)
        arrival_minute=arr.hour*60+arr.minute;dep_minute=dep.hour*60+dep.minute
        loss=(arr.date()-dep.date()).days*1440+max(0,arrival_minute-9*60) if leg=='outbound' else max(0,18*60-dep_minute)+(360 if night else 0)
        row.update(args=record['args'],queried_at=record['queried_at'],transport_kind=record['kind'],travel_minutes=round(duration),
                   door_minutes=round(duration)+(300 if record['kind']=='flight' else 180),play_loss=loss,overnight=night,
                   duration_text=f'{int(duration//60)}小时{int(duration%60)}分',overnight_label='跨夜抵达' if night else '当天抵达')
        result.append(row)
    return result


def deduplicate(rows):
    unique={}
    for r in rows:
        raw=r['raw'];key=(r['transport_kind'],r['name'],raw.get('departureTime'),raw.get('arrivalTime'),raw.get('departStationName'),raw.get('destStationName'))
        old=unique.get(key)
        if old is None or str(r['queried_at'])>str(old['queried_at']):unique[key]=r
    return list(unique.values())


def metrics(row):return (float(Decimal(row['unit'])),float(row['door_minutes']),float(row['play_loss']))


def pareto(rows,limit=24):
    # 不保留同时间同车次更贵、且在当前偏好中没有额外收益的支配选项。
    nondominated=[]
    for row in rows:
        value=metrics(row)
        if not any(all(a<=b for a,b in zip(metrics(other),value)) and any(a<b for a,b in zip(metrics(other),value)) for other in rows):
            nondominated.append(row)
    unique=[];seen=set()
    for priority in ('economy','balanced','fast'):
        for row in ranked(nondominated,priority)[:8]:
            token=(row['transport_kind'],row['name'],row['raw'].get('departureTime'))
            if token not in seen:unique.append(row);seen.add(token)
    return unique[:limit]


def score_values(values,priority):
    if not values:return []
    mins=[min(x[i] for x in values) for i in range(3)];maxs=[max(x[i] for x in values) for i in range(3)]
    weights=(.45,.35,.20) if priority=='balanced' else (.05,.75,.20)
    scores=[]
    for value in values:
        norm=[(value[i]-mins[i])/(maxs[i]-mins[i]) if maxs[i]>mins[i] else 0 for i in range(3)]
        scores.append(sum(w*v for w,v in zip(weights,norm)))
    return scores


def ranked(rows,priority):
    if priority=='economy':return sorted(rows,key=lambda r:(Decimal(r['unit']),r['door_minutes'],r['play_loss']))
    scores=score_values([metrics(r) for r in rows],priority)
    return [r for _,r in sorted(zip(scores,rows),key=lambda x:(x[0],Decimal(x[1]['unit']),x[1]['play_loss']))]


def reason(row,priority):
    labels={'economy':'以单人参考票价为首要排序，接受符合限制的较长车程。',
            'balanced':'综合参考票价、包含接驳预留的时长和占用游玩时间进行比较。',
            'fast':'优先缩短包含接驳预留的单程时长，并兼顾抵达时段。'}
    return labels.get(priority,labels['balanced'])+'仅比较本次取得的候选；'+('为跨夜行程，景点按实际抵达日重新安排。' if row['overnight'] else '为当天抵达行程。')


def choose_one(rows,priority='balanced',price_cap=None):
    if not rows:return None
    within=[r for r in rows if price_cap is None or Decimal(r['unit'])<=price_cap]
    pool=within or rows
    row=copy.deepcopy(ranked(pool,priority)[0])
    row['selection_reason']=reason(row,priority)
    row['priority_label']=PRIORITIES.get(priority,'均衡选择')
    return row


def budget_limit(p):
    nights=p.get('nights') if p.get('nights') is not None else p['days']-1
    from .stay_policy import lodging_cost
    basic=lodging_cost(p,nights)+(p['food']+p['transport'])*p['people']*p['days']
    tickets=p['unknown_ticket']*p['people']*max(1,p['days']-1)*2
    available=max(Decimal(0),p['budget']*Decimal('.9')-basic-tickets)
    ratio=p.get('transport_budget_pct')
    if ratio is not None:available=min(available,p['budget']*Decimal(str(ratio))/100)
    return available/p['people']


def alternatives(rows,selected=None,cap=None):
    result=[];seen=set()
    ordered=[]
    if selected:ordered.append(('当前推荐',selected))
    for priority in ('economy','balanced','fast'):
        row=choose_one(rows,priority,cap)
        if row:ordered.append((PRIORITIES[priority],row))
    for label,r in ordered:
        token=(r['transport_kind'],r['name'],r['raw'].get('departureTime'))
        if token in seen:continue
        seen.add(token);entry=copy.deepcopy(r);entry['option_label']=label
        result.append(entry)
    return result[:4]


def choose_pair(outbound,inbound,p):
    priority=p.get('travel_priority') or 'balanced'
    cap=budget_limit(p)
    out=pareto(deduplicate(outbound));back=pareto(deduplicate(inbound))
    pairs=[]
    for a in out:
        for b in back:
            arrive=timestamp(a['raw'].get('arrivalTime'));leave=timestamp(b['raw'].get('departureTime'))
            try:gap=(leave-arrive).total_seconds()/60
            except TypeError:continue
            if gap < (300 if a['transport_kind']=='flight' or b['transport_kind']=='flight' else 180):continue
            fare=Decimal(a['unit'])+Decimal(b['unit'])
            pairs.append((a,b,fare,a['door_minutes']+b['door_minutes'],a['play_loss']+b['play_loss']))
    if not pairs:return None,None,'往返候选的时间无法衔接，请调整日期或交通限制。'
    within=[r for r in pairs if r[2]<=cap]
    warning=''
    if within:pairs=within
    elif p.get('transport_budget_pct') is not None:
        return None,None,'没有组合满足往返交通预算占比限制，请提高占比或放宽交通条件。'
    else:warning='取得的往返组合超过当前预分配的交通预算，最终方案会重新核算是否超出总预算。'
    if priority=='economy':pairs.sort(key=lambda r:(r[2],r[3],r[4]))
    else:
        scores=score_values([(float(r[2]),r[3],r[4]) for r in pairs],priority)
        pairs=[r for _,r in sorted(zip(scores,pairs),key=lambda x:(x[0],x[1][2]))]
    a,b=copy.deepcopy(pairs[0][0]),copy.deepcopy(pairs[0][1])
    for r in (a,b):r.update(selection_reason=reason(r,priority),priority_label=PRIORITIES[priority])
    return a,b,warning


def gather(p,leg,args,query_callable):
    mode=p['travel_kind'];priority=p.get('travel_priority') or 'balanced'
    kinds=('train','flight') if mode=='compare' else (mode,)
    pool=[];reports=[];errors=[]
    for kind in kinds:
        sorts=['5'] if priority=='economy' else ['3'] if priority=='fast' else ['5','3']
        if kind=='train':sorts=list(dict.fromkeys(sorts+['5','3']))
        if kind=='flight':sorts=[None]
        for order in sorts:
            params=dict(args)
            if kind=='train':params['searchType']=order
            try:
                record=query_callable(kind,params)
                found=prepare(record,p['people'],leg,p.get('allow_overnight',True),p.get('max_travel_hours'),p.get('seat_preference') or 'any')
                pool.extend(found)
                if kind=='train' and len(sorts)==1 and (not found or all(Decimal(r['unit'])>budget_limit(p) for r in found)):
                    sorts.append('3' if order=='5' else '5')
                reports.append({'kind':kind,'args':params,'rows':len(record['rows']),'accepted':len(found),'queried_at':record['queried_at']})
            except QuoteError as exc:
                errors.append(str(exc));reports.append({'kind':kind,'args':params,'error':str(exc)})
                if any(word in str(exc) for word in ('额度','429','密钥验证','频率')):break
    if not reports or all('error' in r for r in reports):raise QuoteError('；'.join(dict.fromkeys(errors)) or '未取得交通数据。')
    return deduplicate(pool),reports,errors
