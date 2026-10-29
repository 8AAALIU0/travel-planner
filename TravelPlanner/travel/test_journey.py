from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,Client
from django.utils import timezone
from .journey_forms import JourneyForm
from .journey_service import initial_job,advance,pick_transport
from .journey_views import JOB
from .planner_service import build_plan,fetch_pool
from .planner_api import cache_key
from .models import PlannerCache
from .cost_service import QuoteError
from .test_planner import preferences,rows


def data():
 return dict(origin='上海',city='杭州市',start_date=(timezone.localdate()+timedelta(days=2)).isoformat(),days=3,people=2,budget='5000',keywords='自然风光、博物馆',travel_kind='train',pace='relaxed',mode='transit',hotel='300',rooms=1,nights=2,food='80',transport='30',unknown_ticket='50',fallback_return='600')


def simple_plan(p,key):
 return {'city':p['city'],'start_date':p['start_date'].isoformat(),'requested_days':p['days'],'people':p['people'],'budget':str(p['budget']),
  'days':[{'date':p['start_date'].isoformat(),'number':1,'stops':[{'name':'示例公园0'}]}], 'warnings':[],
  'costs':{'hotel':'600','food':'480','transport':'180','tickets':'100','reserve':'500','intercity':str(p['intercity'])},'total':str(Decimal(1860)+p['intercity']),'remaining':'1000','status':'draft'}


def record(kind,args,price='100'):
 day=args.get('departureDate') or args.get('depart_date')
 if kind=='hotel':return {'kind':kind,'args':args,'queried_at':timezone.now().isoformat(),'rows':[]}
 if kind=='ticket':return {'kind':kind,'args':args,'queried_at':timezone.now().isoformat(),'rows':[]}
 dep,arr=('08:00','10:00') if args['departureCityName']=='上海' else ('19:00','21:00')
 raw={'departureTime':day+' '+dep,'arrivalTime':day+' '+arr,'seatAvailable':{'edzNum':9},'trainNum':'G1'}
 return {'kind':kind,'args':args,'queried_at':timezone.now().isoformat(),'rows':[{'usable':True,'unit':price,'name':'G1 · 二等座','raw':raw,'info':'测试车站 → 测试车站','note':'测试报价'}]}


class JourneyTests(TestCase):
 def test_single_form_validation(self):
  form=JourneyForm(data());self.assertTrue(form.is_valid(),form.errors)
  self.assertEqual(form.cleaned_data['keywords'],['自然风光','博物馆'])
  self.assertNotIn('interests',form.fields)
  bad=data();bad['keywords']='a,b,c,d';self.assertFalse(JourneyForm(bad).is_valid())
  bad=data();bad['nights']=4;self.assertFalse(JourneyForm(bad).is_valid())

 def test_job_all_stages_counts_roundtrip_and_resume(self):
  f=JourneyForm(data());self.assertTrue(f.is_valid());job=initial_job(f.cleaned_data)
  with patch('travel.journey_service.build_plan',side_effect=simple_plan),patch('travel.journey_service.query',side_effect=record) as query:
   for i in range(7):
    self.assertEqual(job['index'],i)
    job=advance(job,'')
  self.assertTrue(job['done']);self.assertEqual(len(job['steps']),7)
  self.assertEqual(query.call_count,7) # 均衡往返4次，酒店空结果补核一次，加一个景点。
  self.assertEqual(Decimal(job['plan']['costs']['intercity']),400)
  self.assertEqual(sum(Decimal(r['amount']) for r in job['breakdown']),Decimal(job['plan']['total']))

 def test_query_failure_completes_with_reserves(self):
  f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data)
  with patch('travel.journey_service.build_plan',side_effect=simple_plan),patch('travel.journey_service.query',side_effect=QuoteError('平台额度不足')):
   for _ in range(7):job=advance(job,'')
  self.assertTrue(job['done']);self.assertTrue(job['warnings'])
  self.assertEqual(Decimal(job['plan']['costs']['intercity']),1200)
  self.assertEqual(job['breakdown'][0]['state'],'估算预留')

 def test_one_way_missing_does_not_zero_return(self):
  f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data);job['index']=6
  args={'departureCityName':'上海','arrivalCityName':'杭州','departureDate':data()['start_date']}
  r=record('train',args);job['prices']['outbound']=pick_transport(r,2,'outbound')
  with patch('travel.journey_service.build_plan',side_effect=simple_plan):job=advance(job,'')
  self.assertEqual(Decimal(job['plan']['costs']['intercity']),800) # (100+600/2)*2

 def test_transport_requires_group_seats_and_same_day(self):
  args={'departureCityName':'上海','arrivalCityName':'杭州','departureDate':data()['start_date']};r=record('train',args)
  self.assertIsNotNone(pick_transport(r,2,'outbound'))
  r['rows'][0]['raw']['seatAvailable']['edzNum']=1
  self.assertIsNone(pick_transport(r,2,'outbound'))
  r['rows'][0]['raw']['seatAvailable']['edzNum']=9
  r['rows'][0]['raw']['arrivalTime']='2025-01-01 10:00'
  self.assertIsNone(pick_transport(r,2,'outbound'))

 def test_arrival_and_return_constrain_real_itinerary(self):
  p=preferences(arrival_minutes=14*60,return_cutoff=10*60,budget=Decimal(10000))
  with patch('travel.planner_service.fetch_pool',return_value=(rows(9),[],0)),patch('travel.planner_service.route_between',return_value={'minutes':10,'km':1,'verified':True,'summary':'测试','fetched_at':timezone.now().isoformat()}):
   result=build_plan(p,'')
  for stop in result['days'][0]['stops']:self.assertGreaterEqual(stop['begin'],'14:00')
  for stop in result['days'][-1]['stops']:self.assertLessEqual(stop['end'],'10:00')

 def test_keywords_actually_change_poi_queries(self):
  with patch('travel.planner_service.search_places',return_value=[]) as query:
   fetch_pool('杭州',['nature','culture'],'KEY',keywords=['博物馆'])
  self.assertEqual(query.call_count,1)
  self.assertEqual(query.call_args.args[2],'博物馆')

 def test_page_does_not_auto_call_on_get(self):
  with patch('travel.journey_service.query') as q:
   r=self.client.get('/journey/');self.assertEqual(r.status_code,200);q.assert_not_called()
  self.assertContains(r,'生成整趟旅行方案')
  self.assertEqual(self.client.get('/journey/export/').status_code,404)

 def test_workflow_request_is_owned_and_idempotent(self):
  self.assertEqual(self.client.post('/journey/step/',{'index':0}).status_code,404)
  self.client.post('/journey/',data())
  self.assertIn(JOB,self.client.session)
  with patch('travel.journey_views.advance',side_effect=lambda job,key:{**job,'index':1,'steps':[{'name':'测试','message':'完成'}]}) as work:
   self.assertEqual(self.client.post('/journey/step/',{'index':0}).status_code,200)
   self.client.post('/journey/step/',{'index':0})
   self.assertEqual(work.call_count,1)
  self.assertEqual(Client().post('/journey/step/',{'index':1}).status_code,404)
  self.assertEqual(Client(enforce_csrf_checks=True).post('/journey/step/',{'index':0}).status_code,403)

 def test_two_tabs_running_guard(self):
  self.client.post('/journey/',data());key=self.client.session[JOB]
  item=PlannerCache.objects.get(cache_key=key);job=item.payload;job.update(running=True,started=timezone.now().timestamp());item.payload=job;item.save()
  with patch('travel.journey_views.advance') as work:
   r=self.client.post('/journey/step/',{'index':0});self.assertTrue(r.json()['running']);work.assert_not_called()
