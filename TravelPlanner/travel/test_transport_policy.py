from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .cost_service import normalize,QuoteError
from .transport_policy import prepare,choose_one,choose_pair,gather,alternatives
from .journey_forms import JourneyForm,TransportPreferenceForm
from .journey_service import restore,initial_job,advance,lodging_window
from .journey_views import JOB
from .models import PlannerCache
from .test_journey import data,simple_plan


def day(offset=2):return (timezone.localdate()+timedelta(days=offset)).isoformat()


def train_record(items):
 args={'departureDate':day(),'departureCityName':'北京','arrivalCityName':'杭州'}
 raw=[]
 for name,price,dep,arr,code in items:
  raw.append({'trainNum':name,'departureTime':dep,'arrivalTime':arr,'price':{code+'Price':str(price)},'seatAvailable':{code+'Num':99},'departStationName':'北京南','destStationName':'杭州东'})
 return {'kind':'train','args':args,'rows':normalize('train',{'data':raw},args),'queried_at':timezone.now().isoformat()}


def pool():
 r=train_record([('Z1',200,day()+' 19:00',day(3)+' 11:00','yw'),('D1',400,day()+' 06:00',day()+' 14:00','edz'),('G1',650,day()+' 08:00',day()+' 12:00','edz')])
 return prepare(r,2,'outbound')


def prefs(**kw):
 f=JourneyForm(data());assert f.is_valid(),f.errors
 p=restore(initial_job(f.cleaned_data)['input']);p.update(kw)
 return p


class TransportPolicyTests(TestCase):
 def test_three_preferences_choose_different_tradeoffs(self):
  choices=pool()
  self.assertEqual(choose_one(choices,'economy')['unit'],'200.00')
  self.assertEqual(choose_one(choices,'balanced')['unit'],'400.00')
  self.assertEqual(choose_one(choices,'fast')['unit'],'650.00')

 def test_crossnight_seats_and_duration_limits(self):
  record=train_record([('Z1',200,day()+' 19:00',day(3)+' 11:00','yw'),('G1',650,day()+' 08:00',day()+' 12:00','edz')])
  self.assertEqual(len(prepare(record,2,'outbound',allow_overnight=False)),1)
  self.assertEqual(prepare(record,2,'outbound',max_hours=6)[0]['name'],'G1 · 二等座')
  self.assertEqual(prepare(record,2,'outbound',seat_preference='sleeper')[0]['name'],'Z1 · 硬卧')

 def test_budget_applies_to_whole_roundtrip_not_half_per_leg(self):
  out=pool()[-1];out={**out,'unit':'700'}
  back={**pool()[-1],'unit':'100','raw':{**pool()[-1]['raw'],'departureTime':day(4)+' 18:00','arrivalTime':day(4)+' 22:00'}}
  p=prefs(budget=Decimal(1000),people=1,days=1,nights=0,hotel=Decimal(0),food=Decimal(0),transport=Decimal(0),unknown_ticket=Decimal(1),travel_priority='economy')
  a,b,w=choose_pair([out],[back],p)
  self.assertIsNotNone(a);self.assertEqual(Decimal(a['unit'])+Decimal(b['unit']),800)

 def test_explicit_transport_budget_limit_is_respected(self):
  out=pool()[-1];back={**out,'raw':{**out['raw'],'departureTime':day(4)+' 18:00','arrivalTime':day(4)+' 22:00'}}
  a,b,w=choose_pair([out],[back],prefs(transport_budget_pct=10))
  self.assertIsNone(a);self.assertIsNone(b);self.assertIn('预算占比',w)

 def test_economy_queries_price_and_balanced_queries_both_pools(self):
  def query(kind,args):return train_record([('G1',100,day()+' 08:00',day()+' 12:00','edz')])
  args={'departureDate':day(),'departureCityName':'北京','arrivalCityName':'杭州'}
  with patch('travel.journey_service.query',side_effect=query) as q:
   gather(prefs(travel_priority='economy'),'outbound',args,q)
   self.assertEqual(q.call_args_list[0].args[1]['searchType'],'5');self.assertEqual(q.call_count,2)
  with patch('travel.journey_service.query',side_effect=query) as q:
   gather(prefs(travel_priority='balanced'),'outbound',args,q)
   self.assertEqual({c.args[1]['searchType'] for c in q.call_args_list},{'5','3'})

 def test_cheapest_pool_filtered_out_can_fall_back_to_fast_pool(self):
  def query(kind,args):
   if args['searchType']=='5':return train_record([('Z1',200,day()+' 19:00',day(3)+' 11:00','yw')])
   return train_record([('G1',600,day()+' 08:00',day()+' 12:00','edz')])
  p=prefs(travel_priority='economy',allow_overnight=False)
  found,reports,errors=gather(p,'outbound',{'departureDate':day()},query)
  self.assertEqual(len(reports),2);self.assertEqual(found[0]['name'],'G1 · 二等座')

 def test_plane_failure_does_not_discard_train_results(self):
  def query(kind,args):
   if kind=='flight':raise QuoteError('飞机服务权限暂不可用')
   return train_record([('G1',100,day()+' 08:00',day()+' 12:00','edz')])
  rows,reports,errors=gather(prefs(travel_kind='compare'),'outbound',{'departureDate':day()},query)
  self.assertTrue(rows);self.assertTrue(errors)

 def test_alternatives_are_deduplicated(self):
  options=alternatives(pool(),choose_one(pool(),'balanced'))
  keys=[(x['name'],x['raw']['departureTime']) for x in options]
  self.assertEqual(len(keys),len(set(keys)));self.assertGreaterEqual(len(keys),3)

 def make_job(self):
  f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data)
  choice=pool()[0]
  job.update(done=True,index=7,transport_options={'outbound':[choice]},prices={},plan=simple_plan(restore(job['input']),''))
  item=PlannerCache.objects.create(cache_key='b'*64,payload=job,expires_at=timezone.now()+timedelta(hours=1))
  session=self.client.session;session[JOB]=item.cache_key;session.save()
  return item

 def test_preferences_update_only_transport_flow(self):
  item=self.make_job()
  response=self.client.post('/journey/transport-preferences/',{'transport-travel_priority':'economy','transport-travel_kind':'train','transport-allow_overnight':'on','transport-seat_preference':'sleeper','transport-max_travel_hours':24})
  self.assertRedirects(response,'/journey/')
  item.refresh_from_db();job=item.payload
  self.assertEqual(job['index'],1);self.assertEqual(job['input']['travel_priority'],'economy')
  self.assertTrue(job['retry_transport_only']);self.assertFalse(job['done'])

 def test_switch_candidate_uses_server_price_and_replans_arrival(self):
  item=self.make_job()
  response=self.client.post('/journey/select-transport/',{'leg':'outbound','index':0,'unit':'0'})
  self.assertRedirects(response,'/journey/')
  item.refresh_from_db();job=item.payload
  self.assertEqual(job['prices']['outbound']['unit'],'200.00');self.assertEqual(job['index'],3)
  with patch('travel.journey_service.build_plan',side_effect=simple_plan) as build,patch('travel.journey_service.query',return_value={'rows':[],'queried_at':timezone.now().isoformat()}):
   result=job
   while not result['done']:result=advance(result,'')
   self.assertEqual(build.call_args.args[0]['arrival_day_offset'],1)
   self.assertEqual(build.call_args.args[0]['arrival_minutes'],12*60+30)
  self.assertTrue(result['done'])

 def test_expired_candidate_requires_requery(self):
  item=self.make_job();job=item.payload
  job['transport_options']['outbound'][0]['queried_at']=(timezone.now()-timedelta(hours=1)).isoformat();item.payload=job;item.save()
  self.client.post('/journey/select-transport/',{'leg':'outbound','index':0})
  item.refresh_from_db();self.assertTrue(item.payload['done']);self.assertFalse(item.payload['prices'])

 def test_invalid_preference_does_not_start_query(self):
  item=self.make_job()
  self.client.post('/journey/transport-preferences/',{'transport-travel_priority':'economy','transport-travel_kind':'bus','transport-transport_budget_pct':101})
  item.refresh_from_db();self.assertTrue(item.payload['done'])


 def test_auto_lodging_follows_overnight_arrival(self):
  p=prefs(nights=None)
  job={'prices':{'outbound':pool()[0]}}
  lodging=lodging_window(job,p)
  self.assertEqual(lodging['checkIn'],day(3))
  self.assertEqual(lodging['checkOut'],day(4))
  self.assertEqual(lodging['nights'],1)

 def test_pair_with_return_before_arrival_is_rejected(self):
  out=pool()[0]
  back={**pool()[-1],'raw':{**pool()[-1]['raw'],'departureTime':day()+' 18:00','arrivalTime':day()+' 22:00'}}
  a,b,warning=choose_pair([out],[back],prefs())
  self.assertIsNone(a);self.assertIsNone(b);self.assertIn('无法衔接',warning)
