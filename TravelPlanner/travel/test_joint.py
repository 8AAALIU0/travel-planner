from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from .test_transport_policy import prefs,pool,day
from .test_planner import rows
from .joint_planner import select_joint,parameters,validate_plan,goals
from .planner_service import enrich,build_plan
from .itinerary_optimizer import search
from .test_optimizer import route


def choices():
    base=pool()[-1]
    night={**base,'name':'Z夜间','unit':'100','raw':{**base['raw'],'departureTime':day()+' 19:00','arrivalTime':day(3)+' 11:20'},'travel_minutes':980,'door_minutes':1160,'play_loss':1580}
    morning={**base,'name':'G早到','unit':'300','raw':{**base['raw'],'departureTime':day()+' 06:00','arrivalTime':day()+' 10:00'},'travel_minutes':240,'door_minutes':420,'play_loss':60}
    early={**base,'name':'G早返','unit':'100','raw':{**base['raw'],'departureTime':day(4)+' 09:00','arrivalTime':day(4)+' 13:00'},'travel_minutes':240,'door_minutes':420,'play_loss':540}
    late={**base,'name':'G晚返','unit':'300','raw':{**base['raw'],'departureTime':day(4)+' 19:00','arrivalTime':day(4)+' 23:00'},'travel_minutes':240,'door_minutes':420,'play_loss':0}
    return [night,morning],[early,late]


class JointTests(TestCase):
    def run_joint(self,p,out=None,back=None,verifier=None):
        a,b=choices()
        with patch('travel.joint_planner.fetch_pool',return_value=(rows(9),[],0)):
            return select_joint(out or a,back or b,p,'',verifier=verifier)

    def test_economy_protects_three_play_days_not_cheapest_empty_travel_days(self):
        p=prefs(pace='normal',budget=Decimal(10000),travel_priority='economy')
        a,b,r=self.run_joint(p)
        self.assertEqual(a['name'],'G早到');self.assertEqual(b['name'],'G晚返')
        self.assertTrue(r['met']);self.assertEqual(r['play_days'],3)
        self.assertTrue(all(r['counts']))

    def test_user_can_explicitly_accept_one_play_day_to_save_money(self):
        p=prefs(pace='normal',budget=Decimal(10000),travel_priority='economy',minimum_play_days=1,minimum_sights=1)
        a,b,r=self.run_joint(p)
        self.assertEqual(a['name'],'Z夜间');self.assertEqual(b['name'],'G早返')
        self.assertTrue(r['met']);self.assertEqual(r['counts'][0],0);self.assertEqual(r['counts'][2],0)

    def test_only_bad_connections_are_reported_unmet_not_filled_with_fake_stops(self):
        out,back=choices();a,b,r=self.run_joint(prefs(budget=Decimal(10000)),out[:1],back[:1])
        self.assertFalse(r['met']);self.assertEqual(r['counts'][0],0);self.assertEqual(r['counts'][2],0)

    def test_explicit_fare_cap_still_wins_over_sightseeing(self):
        a,b,r=self.run_joint(prefs(budget=Decimal(1000),transport_budget_pct=1))
        self.assertIsNone(a);self.assertIn('预算',r['error'])

    def test_actual_route_failure_checks_next_pair(self):
        p=prefs(pace='normal',budget=Decimal(10000))
        calls=[]
        def verify(q,key):
            calls.append(q)
            counts=[0,1,0] if len(calls)==1 else [1,1,1]
            return {'days':[{'stops':[{}]*n} for n in counts],'warnings':[]}
        a,b,r=self.run_joint(p,verifier=verify)
        self.assertEqual(len(calls),2);self.assertTrue(r['met']);self.assertFalse(r['preview'])

    def test_final_validation_rejects_degraded_plan(self):
        p=prefs();plan={'days':[{'stops':[]},{'stops':[{},{}]},{'stops':[]}],'warnings':[]}
        check=validate_plan(plan,p)
        self.assertFalse(check['met']);self.assertEqual(check['counts_text'],'0 / 2 / 0')
        self.assertIn('目标未满足',plan['warnings'][0])

    def test_default_goals_and_actual_schedule_spread_across_days(self):
        p=prefs(pace='normal',budget=Decimal(10000));out,back=choices();q=parameters(p,out[1],back[1])
        self.assertEqual(goals(q),(3,3))
        with patch('travel.planner_service.fetch_pool',return_value=(rows(9),[],0)),patch('travel.planner_service.route_between',side_effect=route):
            plan=build_plan(q,'')
        self.assertTrue(validate_plan(plan,q)['met'])

    def test_duration_buffer_same_for_preview_and_final(self):
        out,back=choices();q=parameters(prefs(nights=None),out[0],back[0])
        self.assertEqual(q['arrival_day_offset'],1);self.assertEqual(q['arrival_minutes'],12*60+50)
        self.assertEqual(q['return_cutoff'],7*60);self.assertEqual(q['nights'],1)
