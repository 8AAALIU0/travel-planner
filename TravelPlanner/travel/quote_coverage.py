"""Report evidence coverage separately from successful itinerary generation."""
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def recent(value,seconds):
    stamp=parse_datetime(str(value or ''))
    return bool(stamp and timezone.is_aware(stamp) and 0<=(timezone.now()-stamp).total_seconds()<=seconds)


def coverage(job,plan):
    prices=job.get('prices',{})
    fresh=sum(bool(prices.get(leg)) and recent(prices[leg].get('queried_at'),900) for leg in ('outbound','inbound'))
    stops=[s for d in plan.get('days',[]) for s in d.get('stops',[])]
    sourced=sum(s.get('price_reference',False) or s.get('ticket_estimate',{}).get('kind')=='platform' or s.get('tuniu_reference',False) for s in stops)
    complete=bool(fresh==2 and not job.get('transport_pair_unresolved') and stops and sourced==len(stops) and plan.get('unverified_legs',0)==0)
    return {'transport_fresh':fresh,'ticket_sourced':sourced,'ticket_estimated':len(stops)-sourced,
            'title':'路线已生成，参考资料较完整' if complete else '路线已生成，部分票价或交通仍待核实',
            'note':'票价资料、平台起价和查询时余票均不等于指定日期的实时可预订库存；预订前需再核实。'}
