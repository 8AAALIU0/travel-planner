"""Feasibility, objective, evidence and bounded-retry regressions."""
import copy
from decimal import Decimal
from datetime import timedelta
from itertools import permutations
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .itinerary_optimizer import search,select_candidates,improve,rank
from .planner_service import enrich,build_plan
from .query_runtime import planning_step
from .journey_service import initial_job,restore,ticket_stage,advance
from .journey_forms import JourneyForm
from .cost_service import QuoteError
from .quote_coverage import coverage
from .ticket_budgeting import options_for
from .test_journey import data,simple_plan,record
from .test_planner import rows,preferences
from .test_optimizer import route


class QualityPlanningTests(TestCase):
    def candidates(self,costs,scores=None,known=True):
        p=preferences(people=1,days=1,pace='relaxed',budget=Decimal(10000),budget_strategy='experience')
        candidates=sorted(enrich(rows(len(costs)),p),key=lambda r:int(r["poi_id"][1:]))
        for i,row in enumerate(candidates):
            row.update(ticket_per_person=str(costs[i]),visit_minutes=60,score=(scores or [5]*len(costs))[i],
                       price_reference=known,hours_week='09:00-17:00')
        return p,candidates

    def test_small_case_agrees_with_exhaustive_oracle(self):
        p,candidates=self.candidates([10,20,30,40])
        state,_=search(candidates,p,Decimal(60))
        state,attempts=improve(state,candidates,p,Decimal(60),{}, {},max_evaluations=150)
        # Independent brute force for this constant-duration, complete tiny graph.
        feasible=[pair for pair in permutations(range(4),2) if sum(int(candidates[i]['ticket_per_person']) for i in pair)<=60]
        expected=max(sum(int(candidates[i]['ticket_per_person']) for i in pair) for pair in feasible)
        self.assertEqual(state.cost,expected*100)
        self.assertEqual(sum(map(len,state.days)),2)
        self.assertLessEqual(attempts,150)

    def test_budget_fill_cannot_trade_away_quality(self):
        p,candidates=self.candidates([10,10,80],[20,20,0])
        state,_=search(candidates,p,Decimal(100))
        self.assertEqual({s['poi_id'] for d in state.days for s in d},{'P0','P1'})
        self.assertEqual(state.cost,2000)

    def test_unknown_high_estimate_is_not_an_upgrade(self):
        p,candidates=self.candidates([10,10,80],known=False)
        state,_=search(candidates,p,Decimal(100))
        self.assertEqual(state.cost,2000)
        self.assertEqual(state.reference_spend,0)

    def test_diversified_candidate_pool_keeps_low_cost_theme_outside_top24(self):
        p,candidates=self.candidates([100]*39+[1])
        for row in candidates:row['keyword_matches']=['自然风光']
        candidates[-1]['keyword_matches']=['博物馆']
        selected=select_candidates(candidates,32)
        self.assertEqual(len(selected),32)
        self.assertIn('P39',{s['poi_id'] for s in selected})
        state,_=search(selected,p,Decimal(2))
        self.assertEqual([s['poi_id'] for d in state.days for s in d],['P39'])

    def test_insert_replace_search_preserves_windows_budget_and_uniqueness(self):
        p,candidates=self.candidates([10,20,30,40])
        p.update(days=2,_minimum_play_days=2,_minimum_sights=2,arrival_minutes=15*60,return_cutoff=12*60)
        state,_=search(candidates,p,Decimal(50))
        best,attempts=improve(state,candidates,p,Decimal(50),{}, {},max_evaluations=100)
        self.assertLessEqual(best.cost,5000)
        ids=[s['poi_id'] for d in best.days for s in d]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue(all(s['begin']>='15:00' for s in best.days[0]))
        self.assertTrue(all(s['end']<='12:00' for s in best.days[-1]))
        self.assertGreaterEqual(rank(best,2,2)[:2],rank(state,2,2)[:2])
        self.assertLessEqual(attempts,100)

    def test_ticket_connection_failure_does_not_skip_next_success(self):
        form=JourneyForm(data());self.assertTrue(form.is_valid());job=initial_job(form.cleaned_data);p=restore(job['input'])
        day=p['start_date'].isoformat();plan={'days':[{'date':day,'stops':[{'name':'公园A'},{'name':'公园B'}]}]}
        good={'rows':[],'queried_at':timezone.now().isoformat()}
        with patch('travel.journey_service.build_plan',return_value=plan),patch('travel.journey_service.query',side_effect=[QuoteError('错误码 101'),good]) as api:
            finished,_=ticket_stage(job,p,'',4);self.assertFalse(finished)
            self.assertFalse(job.get('ticket_queries_paused'))
            finished,_=ticket_stage(job,p,'',4);self.assertTrue(finished)
            self.assertEqual(api.call_count,2)
            self.assertEqual(api.call_args.args[1]['scenic_name'],'公园B')
        with patch('travel.journey_service.build_plan',return_value=plan),patch('travel.journey_service.query',return_value=good) as retry:
            ticket_stage(job,p,'',5)
            self.assertEqual(retry.call_args.args[1]['scenic_name'],'公园A')
            self.assertEqual(job['ticket_attempts']['公园A|'+day],2)

    def test_final_new_stop_receives_bounded_reconciliation(self):
        form=JourneyForm(data());form.is_valid();job=initial_job(form.cleaned_data);job['index']=6
        job['ticket_checked']=['旧景点|'+data()['start_date']]
        with patch('travel.journey_service.build_plan',side_effect=simple_plan):
            job=advance(job,'');self.assertEqual(job['index'],5)
            job.update(index=6,ticket_reconcile_rounds=2)
            job=advance(job,'');self.assertTrue(job['done'])
            self.assertEqual(job['plan']['quote_coverage']['transport_fresh'],0)
            self.assertIn('待核实',job['plan']['quote_coverage']['title'])

    def test_quote_coverage_does_not_accept_stale_fares_as_fresh(self):
        old=(timezone.now()-timedelta(minutes=16)).isoformat()
        plan={'days':[{'stops':[{'price_reference':True},{'ticket_estimate':{'kind':'estimate'}}]}]}
        report=coverage({'prices':{'outbound':{'queried_at':old},'inbound':{'queried_at':timezone.now().isoformat()}}},plan)
        self.assertEqual(report['transport_fresh'],1)
        self.assertEqual(report['ticket_sourced'],1)
        self.assertEqual(report['ticket_estimated'],1)
        self.assertIn('待核实',report['title'])

    def test_ticket_product_must_match_identity_and_not_be_a_bundle(self):
        target={'name':'目标公园','city':'杭州市'}
        r={'usable':True,'unit':'10','name':'成人票','raw':{'scenicName':'目标公园','cityName':'杭州市'}}
        self.assertEqual(options_for({'rows':[r]},target),[r])
        for raw,name in [({},'成人票'),({'scenicName':'别的景点'},'成人票'),({'scenicName':'目标公园','cityName':'北京'},'成人票'),({'scenicName':'目标公园'},'成人票酒店套票')]:
            wrong={**r,'raw':raw,'name':name}
            self.assertFalse(options_for({'rows':[wrong]},target))

    def test_duplicate_plan_in_same_step_reuses_work_not_shared_across_steps(self):
        p=preferences(days=1)
        @planning_step
        def step(job):
            a=build_plan(p,'TEST');a['warnings'].append('mutated')
            b=build_plan(p,'TEST');self.assertNotIn('mutated',b['warnings'])
        with patch('travel.planner_service.fetch_pool',return_value=(rows(4),[],0)) as places,patch('travel.planner_service.route_between',side_effect=route):
            step({'index':0});self.assertEqual(places.call_count,1)
            step({'index':1});self.assertEqual(places.call_count,2)

    def test_budget_usage_keeps_reserve_and_does_not_fabricate_spending(self):
        p=preferences(days=1,budget=Decimal(6000),budget_strategy='experience')
        with patch('travel.planner_service.fetch_pool',return_value=(rows(4),[],0)),patch('travel.planner_service.route_between',side_effect=route):
            plan=build_plan(p,'')
        usage=plan['budget_use']
        self.assertEqual(Decimal(usage['spend_limit']),5400)
        self.assertEqual(Decimal(plan['costs']['reserve']),600)
        self.assertEqual(Decimal(usage['planned'])+Decimal(usage['unallocated']),5400)
        self.assertGreater(Decimal(usage['unallocated']),0)

    def test_twelve_small_cases_match_exhaustive_quality_budget_frontier(self):
        import random
        for seed in range(12):
            with self.subTest(seed=seed):
                rng=random.Random(seed)
                costs=[rng.randrange(1,9)*10 for _ in range(6)]
                scores=[rng.randrange(1,9) for _ in range(6)]
                p,candidates=self.candidates(costs,scores)
                known={(a['poi_id'],b['poi_id']):route() for a in candidates for b in candidates if a!=b}
                budget=100
                options=[]
                for order in permutations(range(6),2):
                    cost=sum(costs[i] for i in order)
                    if cost<=budget:options.append((206.5+5*sum(scores[i] for i in order),cost))
                best_quality=max(q for q,c in options)
                expected_spend=max(c for q,c in options if q>=best_quality*.97)
                state,_=search(candidates,p,Decimal(budget),known=known)
                state,_=improve(state,candidates,p,Decimal(budget),known,{},max_evaluations=200)
                self.assertEqual(state.cost,expected_spend*100)
                self.assertGreaterEqual(state.utility,best_quality*.97)
