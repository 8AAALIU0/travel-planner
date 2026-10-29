from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .planner_service import build_plan,enrich
from .itinerary_optimizer import optimize,search
from .test_planner import preferences,rows
from .journey_service import pick_transport
from .test_journey import record,data


def route(*args,**kwargs):
 return {'minutes':10,'km':1,'verified':True,'summary':'测试公交','fetched_at':timezone.now().isoformat()}


class OptimizerTests(TestCase):
 def plan(self,p,pool,callback=route):
  with patch('travel.planner_service.fetch_pool',return_value=(pool,[],0)),patch('travel.planner_service.route_between',side_effect=callback):return build_plan(p,'TEST_KEY')

 def test_local_cluster_beats_remote_high_ranked_first_choice(self):
  pool=rows(4)
  pool[0].update(name='高分远方公园',rating='5',longitude=122)
  p=preferences(days=1,budget=Decimal(10000),pace='normal')
  def actual(key,a,b,*args,**kw):
   return {**route(),'minutes':150 if abs(a['longitude']-b['longitude'])>.5 else 10}
  result=self.plan(p,pool,actual)
  stops=result['days'][0]['stops']
  self.assertEqual(len(stops),3)
  self.assertNotIn('高分远方公园',[s['name'] for s in stops])

 def test_afternoon_does_not_add_second_lunch(self):
  p=preferences(days=1,budget=Decimal(10000),pace='normal',arrival_minutes=12*60+50)
  result=self.plan(p,rows(4))
  stops=result['days'][0]['stops']
  self.assertEqual(len(stops),2)
  self.assertTrue(all(s['begin']>='12:50' for s in stops))
  self.assertTrue(all(s['end']<='18:00' for s in stops))
  self.assertFalse(any(s.get('lunch_before') for s in stops))

 def test_actual_slow_edge_repaired_by_other_order(self):
  def actual(key,a,b,*args,**kw):
   return {**route(),'minutes':150 if (a['poi_id'],b['poi_id'])==('P0','P1') else 10}
  result=self.plan(preferences(days=1,pace='normal',budget=Decimal(10000)),rows(4),actual)
  stops=result['days'][0]['stops']
  self.assertEqual(len(stops),3)
  self.assertTrue(all(s['leg']['minutes']<=90 for s in stops if s.get('leg')))
  self.assertLessEqual(result['optimizer']['route_requests'],20)

 def test_in_transit_day_not_filled_and_half_day_can_fit_two(self):
  p=preferences(pace='normal',budget=Decimal(10000),arrival_day_offset=1,arrival_minutes=12*60+50,return_cutoff=11*60+30)
  result=self.plan(p,rows(9))
  self.assertEqual([len(d['stops']) for d in result['days']],[0,2,1])
  self.assertEqual(result['days'][0]['available_minutes'],0)
  self.assertIn('在途',result['days'][0]['schedule_reason'])

 def test_keyword_mapping_parent_child_not_overridden_by_old_default(self):
  pool=rows(2);pool[0].update(name='亲子游乐园',category='体育休闲服务;娱乐场所;游乐园')
  p=preferences(keywords=['自然风光','亲子'])
  result=enrich(pool,p)
  child=next(r for r in result if r['poi_id']=='P0')
  self.assertIn('亲子',child['keyword_matches'])
  self.assertIn('亲子',child['reason'])

 def test_budget_and_no_duplicates_across_multiple_days(self):
  p=preferences(pace='normal',budget=Decimal(2500))
  result=self.plan(p,rows(12))
  self.assertLessEqual(Decimal(result['total']),p['budget'])
  ids=[s['poi_id'] for d in result['days'] for s in d['stops']]
  self.assertEqual(len(ids),len(set(ids)))
  self.assertTrue(all(len(d['stops'])<=3 for d in result['days']))

 def test_fast_day_train_chosen_over_cheap_overnight_within_budget(self):
  args={'departureCityName':'上海','arrivalCityName':'杭州','departureDate':data()['start_date']}
  r=record('train',args,price='600')
  fast=r['rows'][0]
  fast['name']='G1 · 二等座'
  fast['raw']['departureTime']=args['departureDate']+' 08:00'
  fast['raw']['arrivalTime']=args['departureDate']+' 14:00'
  night=__import__('copy').deepcopy(fast);night.update(unit='200',name='Z1 · 硬卧')
  night['raw']['seatAvailable']={'ywNum':9}
  night['raw']['departureTime']=args['departureDate']+' 19:00'
  night['raw']['arrivalTime']=(timezone.localdate()+timedelta(days=3)).isoformat()+' 11:00'
  r['rows']=[night,fast]
  self.assertEqual(pick_transport(r,2,'outbound',price_cap=Decimal(800))['name'],'G1 · 二等座')
  self.assertEqual(pick_transport(r,2,'outbound',price_cap=Decimal(300))['name'],'Z1 · 硬卧')

 def test_unknown_actual_route_not_marked_verified(self):
  from .amap_client import AmapError
  def fail(*a,**kw):raise AmapError('无法连接高德')
  result=self.plan(preferences(days=1,pace='normal',budget=Decimal(10000)),rows(4),fail)
  self.assertEqual(result['verified_legs'],0)
  self.assertGreater(result['unverified_legs'],0)
