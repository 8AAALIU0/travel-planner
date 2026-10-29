"""有来源的成人票参考、平台起价和明确标记的分类预算。"""
import copy
import re
from datetime import date
from decimal import Decimal,ROUND_UP
from django.utils import timezone
from django.utils.dateparse import parse_datetime

CHECKED='2026-10-28'
# 已核对的官方资料。覆盖范围有限，不是全国价格数据库；90天后停用，避免永久沿用旧价。
SOURCES=[
 {'id':'palace','city':'北京','names':['故宫博物院','故宫'],'url':'https://www.dpm.org.cn/singles_detail/257830.html','low':'40','high':'60','season':True,'note':'成人大门票；珍宝馆、钟表馆各另收费，不含在本项。'},
 {'id':'summer','city':'北京','names':['颐和园'],'url':'https://www.summerpalace.net.cn/','low':'20','high':'30','season':True,'note':'成人大门票，园中园、游船等另收费；不等于联票。'},
 {'id':'chimelong','city':'广州','names':['长隆野生动物世界'],'url':'https://bk.chimelong.com/gz/safaripark/','low':'300','high':'300','extra':'50','note':'官网园区页平日标准票300元；特殊日期另有价格，350元为本程序额外保守预留，非已核验特殊日售价。'},
 {'id':'lingyin','city':'杭州','names':['灵隐寺','灵隐飞来峰','灵隐飞来峰景区','飞来峰'],'url':'https://www.hzxcw.gov.cn/content_46075.html','low':'0','high':'0','effective':'2025-12-01','note':'2025年12月1日起免票，仍需实名预约；仅含公告所述入园范围。'},
]


def normalize_name(name):return re.sub(r'[\s()（）·-]','',str(name)).removesuffix('景区')


def official(row,day):
    today=timezone.localdate()
    if not 0<=(today-date.fromisoformat(CHECKED)).days<=90:return None
    name=normalize_name(row['name']);city=str(row.get('city','')).removesuffix('市')
    for s in SOURCES:
        if city!=s['city'] or name not in [normalize_name(x) for x in s['names']]:continue
        if s.get('effective') and day<date.fromisoformat(s['effective']):continue
        low,high=Decimal(s['low']),Decimal(s['high'])
        if s.get('season'):low=high=high if 4<=day.month<=10 else low
        return {'kind':'official','low':str(low),'high':str(high),'basis':'官网/官方公告参考','source_url':s['url'],'checked_at':CHECKED,'note':s['note'],'source_id':s['id'],'extra':s.get('extra','0')}
    return None


def estimate(row,p):
    text=row['name']+row.get('category','');floor=Decimal(str(p['unknown_ticket']))
    if p.get('ticket_policy')=='manual':low=high=floor;label='手动预留'
    else:
        if any(x in text for x in ['迪士尼','环球影城']):low,high,label=Decimal(400),Decimal(1000),'大型主题乐园'
        elif any(x in text for x in ['乐园','游乐场','欢乐谷','宋城','千古情']):low,high,label=Decimal(150),Decimal(450),'游乐或演艺景点'
        elif any(x in text for x in ['动物园','野生动物','海洋馆','水族馆']):low,high,label=Decimal(80),Decimal(350),'动物或海洋场馆'
        elif any(x in text for x in ['博物馆','美术馆','纪念馆']):low,high,label=Decimal(0),Decimal(120),'展馆（未确认免票）'
        elif any(x in text for x in ['古镇','遗址','寺','塔','山岳']):low,high,label=Decimal(30),Decimal(200),'文化或山岳景点'
        elif any(x in text for x in ['公园','湖','湿地','街区','步行街']):low,high,label=Decimal(0),Decimal(150),'公园或开放区域（未确认免票）'
        else:low,high,label=Decimal(50),Decimal(250),'未分类景点'
        high=max(high,floor)
    return {'kind':'estimate','low':str(low),'high':str(high),'basis':'规则估算，未取得票价资料' if p.get('ticket_policy')!='manual' else '手动预算预留',
        'source_url':'','checked_at':'','note':f'{label}；按区间上限预留。不是网站查得的售价，也不保证覆盖所有附加项目。'}


def platform(row,p,day):
    refs=[r for r in p.get('ticket_refs',[]) if r.get('name')==row['name'] and r.get('date')==day.isoformat()]
    for ref in reversed(refs):
        stamp=parse_datetime(ref.get('queried_at',''))
        if not stamp or timezone.is_naive(stamp) or not 0<=(timezone.now()-stamp).total_seconds()<=86400:continue
        try:
            price=Decimal(str(ref['unit']));market=Decimal(str(ref.get('market') or price))
            if not price.is_finite() or price<0:continue
            if not market.is_finite() or market<price or market>10000:market=price
        except (ValueError,ArithmeticError):continue
        high=(max(price,market)*Decimal('1.20')).quantize(Decimal('1'),rounding=ROUND_UP)
        return {'kind':'platform','low':str(price),'high':str(high),'basis':'平台成人票起价参考＋预算余量','source_url':'https://open.tuniu.com/mcp/docs/apidoc/mcp/ticketMCP.html',
            'checked_at':ref['queried_at'],'note':ref['label']+'；起价对应日 '+ref.get('priceday','未知')+'。按已返回起价/门市价较高者加20%作估算，不是指定日期实价或上限保证。'}
    return None


def apply(row,p,day):
    row=copy.deepcopy(row)
    reference=official(row,day)
    if not reference and row.get('price_reference'):
        reference={'kind':'record','low':row['ticket_per_person'],'high':row['ticket_per_person'],'basis':'已维护资料参考','source_url':row.get('ticket_source_url',''),'checked_at':row.get('ticket_checked_on',''),'note':row.get('ticket_type','')}
    if not reference:reference=platform(row,p,day)
    if not reference:reference=estimate(row,p)
    row.update(ticket_estimate=reference,ticket_per_person=str(Decimal(reference['high'])+Decimal(reference.get('extra','0'))),ticket_type=reference['basis']+'；'+reference['note'],
        ticket_source_url=reference['source_url'],price_reference=reference['kind'] in ('official','record'))
    return row


def options_for(record,target):
    name=normalize_name(target['name']);valid=[]
    for r in record['rows']:
        if not r.get('usable') or not r.get('unit'):continue
        raw=r['raw'];scenic=normalize_name(raw.get('scenicName',''))
        if scenic and not (name==scenic or (len(name)>=3 and (name in scenic or scenic in name))):continue
        if any(w in r['name'] for w in ['夜场','下午场','下午票','半日','年卡','次卡','学生','老人','优待','长者','早鸟','夜游']):continue
        valid.append(r)
    return valid
