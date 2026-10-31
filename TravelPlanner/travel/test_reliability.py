from datetime import date,timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .test_journey import data,record
from .test_planner import preferences,rows
from .journey_forms import JourneyForm
from .journey_service import initial_job,restore,advance
from .planner_service import enrich
from .itinerary_optimizer import search,rank
from .path_solver import solve_paths
from .plan_audit import audit_plan
from .transport_policy import evaluate_transport,gather


class ReliabilityTests(TestCase):
    def test_combination_failure_keeps_successful_one_way_quotes(self):
        form=JourneyForm(data());self.assertTrue(form.is_valid());job=initial_job(form.cleaned_data);job['index']=2
        day=data()['start_date'];out=record('train',{'departureDate':day,'departureCityName':'上海','arrivalCityName':'杭州'})
        from .transport_policy import prepare
        job['transport_pools']={'outbound':prepare(out,2,'outbound')}
        job['prices']['outbound']=job['transport_pools']['outbound'][0]
        with patch('travel.journey_service.query',side_effect=record),patch('travel.joint_planner.select_joint',return_value=(None,None,{'error':'没有组合满足预算限制'})):
            result=advance(job,'')
        self.assertIn('outbound',result['prices']);self.assertIn('inbound',result['prices'])
        self.assertTrue(result['transport_pair_unresolved'])

    def test_transport_rejection_reasons_distinguish_unknown_seats(self):
        args={'departureDate':data()['start_date'],'departureCityName':'上海'}
        response=record('train',args)
        response['rows'][0]['raw']['seatAvailable']['edzNum']='未知'
        response['rows'][0]['usable']=False
        accepted,reasons=evaluate_transport(response,2,'outbound')
        self.assertFalse(accepted);self.assertEqual(reasons,{'余票数量未知':1})
        response['rows'][0]['raw']['seatAvailable']['edzNum']=1
        accepted,reasons=evaluate_transport(response,2,'outbound')
        self.assertEqual(reasons,{'余票少于同行人数':1})

    def test_gather_preserves_rejection_counts(self):
        form=JourneyForm(data());form.is_valid();p=restore(initial_job(form.cleaned_data)['input'])
        args={'departureDate':data()['start_date'],'departureCityName':'上海'}
        def query(kind,args):
            result=record(kind,args);result['rows'][0]['raw']['seatAvailable']['edzNum']=0;return result
        accepted,reports,errors=gather(p,'outbound',args,query)
        self.assertFalse(accepted);self.assertFalse(errors)
        self.assertEqual(reports[0]['rejections'],{'余票少于同行人数':1})

    def test_integer_planning_recovers_first_day_and_next_day_from_greedy_loss(self):
        p=preferences(days=2,people=1,start_date=date(2026,11,1),arrival_minutes=14*60,return_cutoff=12*60,
                      _minimum_sights=2,_minimum_play_days=2,budget_strategy='quality')
        candidates=enrich(rows(2),p)
        candidates[0].update(visit_minutes=180,hours_week='周一闭馆',score=1,ticket_per_person='60')
        candidates[1].update(visit_minutes=180,hours_week='09:00-17:00',score=20,ticket_per_person='40')
        seed,_=search(candidates,p,Decimal(100),width=1)
        self.assertEqual(sum(bool(day) for day in seed.days),1)
        result,report=solve_paths(candidates,p,Decimal(100),{}, {},False,seed,seconds=1)
        self.assertEqual([len(day) for day in result.days],[1,1])
        self.assertEqual(result.days[0][0]['poi_id'],candidates[0]['poi_id'])
        self.assertEqual(result.cost,10000)
        self.assertEqual(report['status'],'integer_feasible')
        self.assertTrue(report['quality_optimal_in_pool'])

    def test_unavailable_solver_preserves_incumbent_and_explains_fallback(self):
        p=preferences(days=1);candidates=enrich(rows(2),p);seed,_=search(candidates,p,Decimal(100))
        with patch('pulp.PULP_CBC_CMD.available',return_value=False):
            result,report=solve_paths(candidates,p,Decimal(100),{}, {},False,seed)
        self.assertIs(result,seed);self.assertEqual(report['status'],'solver_unavailable')

    def test_audit_separates_travel_day_from_failed_schedule(self):
        p=preferences(days=3,arrival_day_offset=1,arrival_minutes=10*60)
        plan={'days':[{'stops':[]},{'stops':[]},{'stops':[]}],'candidate_count':5,'remaining':'1000','total':'1000'}
        result=audit_plan({'prices':{}},plan,p)
        self.assertIn('尚未抵达',result['days'][0]['reason'])
        self.assertIn('搜索未找到',result['days'][1]['reason'])
        self.assertIn('不能把余额多解释为优化成功',result['budget_reason'])
        self.assertEqual(result['status'],'needs_review')

    def test_integer_model_honors_closed_edges_and_budget(self):
        p=preferences(days=1,people=1,_minimum_sights=3,budget_strategy='experience')
        candidates=enrich(rows(4),p)
        for r in candidates:r.update(visit_minutes=60,ticket_per_person='40',price_reference=True)
        seed,_=search(candidates,p,Decimal(100),closed=True)
        result,report=solve_paths(candidates,p,Decimal(100),{}, {},True,seed,seconds=1)
        self.assertEqual(sum(map(len,result.days)),1)
        self.assertLessEqual(result.cost,10000)

    def test_page_exposes_missing_transport_reasons_without_claiming_bookable(self):
        response=self.client.get('/journey/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'生成整趟旅行方案')
