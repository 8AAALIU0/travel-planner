"""Independent presentation-time checks; no API calls, no invented inventory."""
from decimal import Decimal
from .planner_service import day_window
from .transport_policy import compatible_pair


def audit_plan(job,plan,p):
    days=[];messages=[];ids=[]
    for index,day in enumerate(plan.get('days',[])):
        start,end=day_window(p,index);stops=day.get('stops',[])
        if index<p.get('arrival_day_offset',0):reason='去程尚未抵达目的地，本日不能安排当地景点。'
        elif start>=end:reason='抵达/返程预留后没有可用游玩时段，请比较更早抵达或更晚返程的班次。'
        elif stops:reason=f'可用约{end-start}分钟，已安排{len(stops)}个景点。'
        elif not plan.get('candidate_count',0):reason='未取得可用景点候选；需核对景点查询，而不是把空白日期算作已完成。'
        else:reason=f'有约{end-start}分钟可用，但当前搜索未找到满足预算、开放时间和通勤条件的组合。'
        days.append({'number':index+1,'reason':reason,'empty':not bool(stops)})
        for stop in stops:
            if stop.get('poi_id'):ids.append(stop['poi_id'])
    if len(ids)!=len(set(ids)):messages.append('核验失败：有重复景点，请重新规划。')
    if Decimal(plan.get('total',0))>p['budget']:messages.append('核验失败：方案预计费用超过总预算。')
    out=job.get('prices',{}).get('outbound');back=job.get('prices',{}).get('inbound')
    if out and back and (not compatible_pair(out,back) or job.get('transport_pair_unresolved')):
        messages.append('核验未通过：单程参考已查到，但完整往返的时间或预算条件尚未确认。')
    elif not out or not back:messages.append('往返交通资料不完整，当前日程仍是暂排，不能确认首末日可玩时段。')
    left=Decimal(plan.get('remaining',0))
    if left<=0:budget_reason='当前没有未分配余额；请注意其中仍可能含估算。'
    elif any(d['empty'] for d in days):budget_reason=f'未分配余额 ¥{left}；目前存在空白游玩日，应先修复交通衔接或景点安排，不能把余额多解释为优化成功。'
    else:budget_reason=f'未分配余额 ¥{left}；预算是费用上限。若每日已达数量或时间上限，继续花钱不会自动增加景点，酒店/餐饮升级需另取得有效报价。'
    return {'days':days,'messages':messages,'budget_reason':budget_reason,
            'status':'needs_review' if messages or any(d['empty'] for d in days) else 'scheduled'}
