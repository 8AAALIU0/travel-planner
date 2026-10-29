from datetime import date,timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .journey_forms import JourneyForm
from .journey_service import initial_job,restore,advance,lodging_window,ticket_stage
from .test_journey import data,record,simple_plan
from .test_planner import rows,preferences
from .test_optimizer import route
from .planner_service import build_plan
from .stay_policy import find_hotels,apply_stay,locate,lodging_cost
from .ticket_budgeting import apply,official,options_for
from .transport_policy import budget_limit
from .joint_planner import remaining,parameters
from .test_joint import choices
from .cost_service import normalize,QuoteError


def p(**kw):
    f=JourneyForm(data());assert f.is_valid(),f.errors
    q=restore(initial_job(f.cleaned_data)['input']);q.update(kw);return q


def ticket_record(name,day,price=320):
    args={'scenic_name':name,'depart_date':day}
    raw={'resName':name+'门票成人票','scenicName':name,'startPrice':str(price),'priceMarket':str(price),'departsDate':day,'startDate':day,'endDate':day}
    return {'rows':normalize('ticket',{'tickets':[raw]},args),'queried_at':timezone.now().isoformat()}


class StayBudgetTests(TestCase):
    def test_free_stay_with_missing_or_invalid_hotel_fields_validates(self):
        values=data();values.update(stay_kind='free',people=6,rooms='wrong',hotel='wrong',nights='wrong')
        f=JourneyForm(values);self.assertTrue(f.is_valid(),f.errors)
        q=restore(initial_job(f.cleaned_data)['input'])
        self.assertEqual(q['hotel'],0);self.assertEqual(q['nights'],0);self.assertEqual(lodging_cost(q),0)

    def test_hotel_requires_room_and_nightly_budget(self):
        values=data();values.pop('hotel');values.pop('rooms')
        self.assertFalse(JourneyForm(values).is_valid())

    def test_free_stay_skips_queries_and_clears_old_candidates(self):
        values=data();values['stay_kind']='free';f=JourneyForm(values);f.is_valid();job=initial_job(f.cleaned_data)
        job.update(index=3,hotels=[{'name':'旧酒店'}])
        with patch('travel.journey_service.query') as query:
            result=advance(job,'');query.assert_not_called()
        self.assertFalse(result['hotels']);self.assertEqual(result['lodging']['nights'],0)
        self.assertTrue(result['hotel_search']['skipped'])

    def test_free_lodging_frees_transport_and_sightseeing_budget(self):
        paid=p();free=apply_stay({**paid,'stay_kind':'free'})
        self.assertGreater(budget_limit(free),budget_limit(paid));self.assertGreater(remaining(free),remaining(paid))
        out,back=choices();self.assertEqual(parameters(free,out[0],back[0])['nights'],0)

    def test_free_final_cost_zero_but_food_and_local_transport_preserved(self):
        q=apply_stay(p(stay_kind='free',budget=Decimal(10000)))
        with patch('travel.planner_service.fetch_pool',return_value=(rows(4),[],0)),patch('travel.planner_service.route_between',side_effect=route):result=build_plan(q,'')
        self.assertEqual(Decimal(result['costs']['hotel']),0)
        self.assertGreater(Decimal(result['costs']['food']),0);self.assertGreater(Decimal(result['costs']['transport']),0)

    def hotel_search(self,preference):
        q=p(hotel=Decimal(250),hotel_preference=preference)
        lodging={'checkIn':q['start_date'].isoformat(),'checkOut':(q['start_date']+timedelta(days=2)).isoformat()}
        hotelrows=[{'name':'远方便宜酒店','unit':'100','raw':{'hotelId':1}}, {'name':'近处酒店','unit':'220','raw':{'hotelId':2}},{'name':'超预算酒店','unit':'600','raw':{'hotelId':3}}]
        def assess(row,*args):return {**row,'commute_minutes':80 if row['name']=='远方便宜酒店' else 15}
        reply={'rows':hotelrows,'queried_at':timezone.now().isoformat(),'data':{}}
        with patch('travel.stay_policy.assess',side_effect=assess),patch('travel.journey_service.query',return_value=reply) as query:
            result,report=find_hotels(q,lodging,rows(1)[0],'',query)
            self.assertEqual(query.call_args.args[1]['prices'],'0-250')
            return result,report

    def test_economy_sorts_cheap_convenience_sorts_actual_commute(self):
        cheap,_=self.hotel_search('economy');near,report=self.hotel_search('convenient')
        self.assertEqual(cheap[0]['name'],'远方便宜酒店');self.assertEqual(near[0]['name'],'近处酒店')
        self.assertNotIn('超预算酒店',[x['name'] for x in cheap]);self.assertEqual(report['eligible'],2)

    def test_same_brand_wrong_branch_not_geocoded_automatically(self):
        poi={'id':'H1','name':'全季酒店乙店','location':'120.1,30.2','cityname':'杭州市','type':'住宿服务;宾馆酒店'}
        with patch('travel.stay_policy.search_places',return_value=[poi]):self.assertIsNone(locate('全季酒店甲店','杭州市','TEST'))

    def test_ticket_category_changes_budget_and_multiplies_people(self):
        q=p(people=4,budget=Decimal(50000),unknown_ticket=Decimal(100))
        park=rows(1)[0];theme={**park,'name':'未知大型游乐园','category':'游乐园'}
        a=apply(park,q,q['start_date']);b=apply(theme,q,q['start_date'])
        self.assertGreater(Decimal(b['ticket_per_person']),Decimal(a['ticket_per_person']))
        with patch('travel.planner_service.fetch_pool',return_value=([theme],[],0)),patch('travel.planner_service.route_between',side_effect=route):result=build_plan(q,'')
        stop=next(s for d in result['days'] for s in d['stops'])
        self.assertEqual(Decimal(stop['group_ticket']),Decimal(stop['ticket_per_person'])*4)
        self.assertEqual(Decimal(result['costs']['tickets']),Decimal(stop['group_ticket']))
        self.assertEqual(stop['ticket_estimate']['kind'],'estimate')

    def test_known_official_free_and_seasonal_ticket_policies(self):
        with patch('travel.ticket_budgeting.timezone.localdate',return_value=date(2026,10,28)):
            palace={'name':'故宫博物院','city':'北京市'}
            self.assertEqual(official(palace,date(2026,10,30))['high'],'60')
            self.assertEqual(official(palace,date(2026,11,1))['high'],'40')
            self.assertEqual(official({'name':'灵隐寺','city':'杭州市'},date(2026,10,30))['high'],'0')
            self.assertIsNone(official({'name':'灵隐寺','city':'其他市'},date(2026,10,30)))
        with patch('travel.ticket_budgeting.timezone.localdate',return_value=date(2027,4,1)):
            self.assertIsNone(official(palace,date(2027,4,2)))

    def test_platform_reference_is_not_swallowed_by_flat_floor(self):
        q=p(unknown_ticket=Decimal(100));row=rows(1)[0]
        q['ticket_refs']=[{'name':row['name'],'date':q['start_date'].isoformat(),'unit':'320','market':'350','label':'普通成人票','priceday':q['start_date'].isoformat(),'queried_at':timezone.now().isoformat()}]
        result=apply(row,q,q['start_date'])
        self.assertEqual(Decimal(result['ticket_per_person']),420)
        self.assertEqual(result['ticket_estimate']['kind'],'platform')
        q['ticket_refs'][0]['date']='2015-01-01';self.assertEqual(apply(row,q,q['start_date'])['ticket_estimate']['kind'],'estimate')

    def test_online_ticket_queue_checks_more_than_two_and_is_resumable(self):
        q=p(budget=Decimal(50000));f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data);job['index']=4
        day=q['start_date'].isoformat()
        draft={'days':[{'date':day,'stops':[{'name':f'景点{i}'} for i in range(7)]}]}
        def query(kind,args):return ticket_record(args['scenic_name'],args['depart_date'])
        with patch('travel.journey_service.build_plan',return_value=draft),patch('travel.journey_service.query',side_effect=query) as api:
            for _ in range(7):job=advance(job,'')
            self.assertEqual(job['index'],5);self.assertEqual(len(job['ticket_refs']),7);self.assertEqual(api.call_count,7)
            job=advance(job,'');self.assertEqual(job['index'],6);self.assertEqual(api.call_count,7)

    def test_no_key_stops_repeated_ticket_api_failures_but_finishes(self):
        q=p();job={'prices':{},'warnings':[]};day=q['start_date'].isoformat()
        draft={'days':[{'date':day,'stops':[{'name':f'景点{i}'} for i in range(4)]}]}
        with patch('travel.journey_service.build_plan',return_value=draft),patch('travel.journey_service.query',side_effect=QuoteError('未填写密钥')) as api:
            for _ in range(4):finished,_=ticket_stage(job,q,'',4)
            self.assertTrue(finished);self.assertEqual(api.call_count,1)

    def test_wrong_scenic_and_night_session_not_adopted(self):
        day=p()['start_date'].isoformat();r=ticket_record('别的景点',day)
        self.assertFalse(options_for(r,{'name':'目标景点','date':day}))
        r=ticket_record('目标景点',day);r['rows'][0]['name']='目标景点夜场成人票'
        self.assertFalse(options_for(r,{'name':'目标景点','date':day}))

    def test_main_page_has_stay_choices_without_network(self):
        with patch('travel.journey_service.query') as api:
            response=self.client.get('/journey/');api.assert_not_called()
        self.assertContains(response,'住亲友家');self.assertContains(response,'价格优先');self.assertContains(response,'门票预算怎样计算')
