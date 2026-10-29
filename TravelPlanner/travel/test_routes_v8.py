from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,Client
from django.utils import timezone
from .cost_service import decode_result,QuoteError,normalize,error_summary
from .route_details import parse_details,static_params,static_map
from .journey_service import pick_transport,advance,initial_job
from .journey_forms import JourneyForm
from .journey_views import JOB
from .models import PlannerCache
from .test_journey import data,record,simple_plan
from .planner_service import day_window


class RouteTests(TestCase):
 def test_platform_error_not_lost_and_key_masked(self):
  obj={'successCode':False,'errorCode':403,'message':'火车票权限未开通'}
  with self.assertRaisesRegex(QuoteError,'403.*火车票权限未开通'):decode_result(__import__('json').dumps(obj))
  self.assertNotIn('sk-PRIVATE123',error_summary({'message':'token=SECRET sk-PRIVATE123'}))
  self.assertNotIn('SECRET',error_summary({'message':'token=SECRET'}))

 def test_time_only_response_parsed_not_dropped(self):
  day=data()['start_date'];raw={'trainNum':'G1','departureTime':'8:00','arrivalTime':'10:00','price':{'edzPrice':'100'},'seatAvailable':{'edzNum':9}}
  args={'departureDate':day,'departureCityName':'上海','arrivalCityName':'杭州'}
  rows=normalize('train',{'data':[raw]},args)
  self.assertTrue(rows[0]['usable'])
  self.assertIsNotNone(pick_transport({'kind':'train','args':args,'rows':rows,'queried_at':timezone.now().isoformat()},2,'outbound'))

 def test_night_train_retained(self):
  day=data()['start_date'];args={'departureDate':day,'departureCityName':'北京','arrivalCityName':'杭州'}
  raw={'trainNum':'Z1','departureTime':day+' 20:00','arrivalTime':(timezone.localdate()+timedelta(days=3)).isoformat()+' 10:00','price':{'ywPrice':'300'},'seatAvailable':{'ywNum':9}}
  rows=normalize('train',{'data':[raw]},args)
  self.assertIsNotNone(pick_transport({'kind':'train','args':args,'rows':rows,'queried_at':timezone.now().isoformat()},2,'outbound'))
  p={'pace':'normal','days':3,'arrival_day_offset':1,'arrival_minutes':11*60}
  self.assertEqual(day_window(p,0)[0],1440)
  self.assertEqual(day_window(p,1)[0],660)

 def test_transport_city_normalization_and_refresh_legacy_hotel_filter(self):
  form=JourneyForm(data());form.is_valid();job=initial_job(form.cleaned_data);job['index']=1
  with patch('travel.journey_service.query',side_effect=record) as api:
   job=advance(job,'')
  self.assertEqual(api.call_args.args[1]['arrivalCityName'],'杭州')
  job.update(index=3,retry_transport_only=True)
  with patch('travel.journey_service.query',side_effect=record) as api:
   job=advance(job,'');self.assertEqual(api.call_args_list[0].args[1]['prices'],'0-300')
  self.assertEqual(job['index'],4)

 def test_empty_filtered_state_is_distinct_from_api_failure(self):
  form=JourneyForm(data());form.is_valid();job=initial_job(form.cleaned_data);job['index']=1
  with patch('travel.journey_service.query',return_value={'kind':'train','args':{},'queried_at':'NOW','rows':[]}):job=advance(job,'')
  self.assertEqual(job['transport_diagnostics']['outbound']['rows'],0)
  self.assertNotIn('error',job['transport_diagnostics']['outbound'])

 def test_bus_full_instructions_and_fare(self):
  selected={'cost':'4','segments':[{'walking':{'steps':[{'instruction':'沿湖滨路步行200米','distance':'200','polyline':'120.1,30.2;120.11,30.21'}]},'bus':{'buslines':[{'name':'地铁1号线','type':'地铁','departure_stop':{'name':'龙翔桥'},'arrival_stop':{'name':'武林广场'},'via_num':'3','polyline':'120.11,30.21;120.12,30.22','distance':'2000'}]}}]}
  parsed=parse_details(selected,'transit')
  self.assertEqual(parsed['fare'],4)
  self.assertIn('龙翔桥',parsed['instructions'][1]['text'])
  self.assertIn('武林广场',parsed['instructions'][1]['text'])
  self.assertEqual(len(parsed['polyline']),3)
  params=static_params(parsed,{'longitude':120.1,'latitude':30.2},{'longitude':120.12,'latitude':30.22})
  self.assertTrue(params['paths'].startswith('6,0x237765,1,,:'))
  self.assertIn('起',params['markers'])

 def test_no_fake_straight_line_if_no_polyline(self):
  from .amap_client import AmapError
  with self.assertRaises(AmapError):static_params({'polyline':[]},{},{})

 def test_detailed_driving_is_parsed(self):
  result=parse_details({'steps':[{'instruction':'沿人民路行驶1公里后右转','distance':1000,'polyline':'120.1,30.2;120.2,30.3'}]},'driving')
  self.assertIn('右转',result['instructions'][0]['text'])
  self.assertIsNone(result['fare'])

 def make_job(self):
  start={'name':'景点甲','longitude':120.1,'latitude':30.2,'poi_id':'A'}
  end={'name':'景点乙','longitude':120.2,'latitude':30.3,'poi_id':'B','leg':{'detail_version':8,'minutes':20,'km':3,'instructions':[],'polyline':[[120.1,30.2],[120.2,30.3]],'verified':True}}
  job={'input':data(),'index':7,'done':True,'running':False,'steps':[],'prices':{},'warnings':[],'plan':{'city':'杭州','mode':'transit','days':[{'stops':[start,end]}]}}
  item=PlannerCache.objects.create(cache_key='a'*64,payload=job,expires_at=timezone.now()+timedelta(hours=1))
  session=self.client.session;session[JOB]=item.cache_key;session.save()
  return item

 def test_route_access_owned_post_only(self):
  self.make_job()
  response=self.client.post('/journey/route/0/1/')
  self.assertEqual(response.status_code,200)
  self.assertEqual(response.json()['from'],'景点甲')
  self.assertEqual(Client().post('/journey/route/0/1/').status_code,404)
  self.assertEqual(self.client.get('/journey/route/0/1/').status_code,405)
  self.assertEqual(self.client.post('/journey/route/0/0/').status_code,404)

 def test_map_proxy_never_embeds_key(self):
  self.make_job()
  with patch('travel.route_details.static_map',return_value=(b'PNG_BYTES','image/png')):
   r=self.client.get('/journey/route/0/1/map/')
  self.assertEqual(r.status_code,200)
  self.assertEqual(r.content,b'PNG_BYTES')
  self.assertIn('private',r['Cache-Control'])

 def test_static_map_json_permission_error(self):
  from .amap_client import AmapError
  class Response:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,n):return b'{"status":"0","infocode":"10005","info":"INVALID_USER_KEY"}'
  class Opener:
   def open(self,*a,**kw):return Response()
  with patch('travel.route_details.build_opener',return_value=Opener()):
   with self.assertRaisesRegex(AmapError,'10005'):
    static_map('NOT_A_REAL_KEY',{'polyline':[[120.1,30.2],[120.2,30.3]]},{'longitude':120.1,'latitude':30.2},{'longitude':120.2,'latitude':30.3})

 def test_completed_existing_job_shows_route_controls(self):
  self.make_job()
  response=self.client.get('/journey/')
  self.assertEqual(response.status_code,200)
  self.assertContains(response,'data-route="/journey/route/0/1/"')
  self.assertContains(response,'重查往返交通并更新方案')
