"""Regression checks for reduced I/O without dropping quote candidates."""
import json
import os
import subprocess
from datetime import timedelta
from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from . import cost_service
from .models import PlannerCache
from .planner_api import cache_key, read_cache_many, write_cache
from .planner_service import fetch_pool
from .query_runtime import planning_step, step_memo
from .test_planner import pois


class QuerySpeedTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_552_route_cache_lookups_use_one_query_and_ignore_expired(self):
        write_cache('fresh', {'minutes': 12, 'verified': True})
        PlannerCache.objects.create(cache_key='expired', payload={'minutes': 1},
                                    expires_at=timezone.now()-timedelta(seconds=1))
        keys=['fresh','expired']+[f'missing-{i}' for i in range(550)]
        with self.assertNumQueries(1):
            result=read_cache_many(keys)
        self.assertEqual(result, {'fresh': {'minutes':12, 'verified':True}})

    def test_pool_does_not_reimport_unchanged_records(self):
        with patch('travel.planner_service.search_places', return_value=pois()):
            original=fetch_pool('杭州市',['nature','culture'],'a'*32)
        with patch('travel.planner_service.import_rows') as save, patch('travel.planner_service.search_places') as api:
            result=fetch_pool('杭州市',['nature','culture'],'a'*32)
        save.assert_not_called()
        api.assert_not_called()
        self.assertEqual(result[0], original[0])

    def test_step_memo_isolated_and_mutation_safe(self):
        calls=[]
        @step_memo
        def source(city):
            calls.append(city)
            return {'names':['park']}
        @planning_step
        def step(job):
            first=source('北京');first['names'].clear()
            self.assertEqual(source('北京')['names'],['park'])
        step({'index':0});step({'index':1})
        self.assertEqual(len(calls),2)

    def test_success_cache_skips_wait_and_provider_even_during_connection_pause(self):
        key='TEST_KEY_NEVER_PRINT';args={'scenic_name':'公园','depart_date':'2026-11-01'}
        import hashlib
        identifier=cache_key('tuniu-quotes-v8',['ticket',args,hashlib.sha256(key.encode()).hexdigest()[:16]])
        record={'rows':[], 'queried_at':timezone.now().isoformat()}
        write_cache(identifier,record,hours=.25)
        with patch.dict(os.environ,{'TUNIU_API_KEY':key}), patch('travel.cost_service.time.sleep') as sleep, patch('travel.cost_service.subprocess.run') as run:
            cache.set(cost_service._connection_key('ticket'),True,60)
            self.assertEqual(cost_service.run_query('ticket',args,wait_for_slot=True),(record,True))
            sleep.assert_not_called();run.assert_not_called()

    def test_connection_failure_pauses_only_same_provider_and_key(self):
        failure=subprocess.CompletedProcess([],101,'','')
        success=subprocess.CompletedProcess([],0,json.dumps({'hotels':[]}), '')
        with patch.dict(os.environ,{'TUNIU_API_KEY':'TEST_KEY'}), patch('travel.cost_service.cli_command',return_value=['node','test.js']), patch('travel.cost_service._last_call',0), patch('travel.cost_service.time.sleep'), patch('travel.cost_service.subprocess.run',side_effect=[failure,success]) as run:
            with self.assertRaisesRegex(cost_service.QuoteError,'101'):
                cost_service.run_query('ticket',{'scenic_name':'A'},wait_for_slot=True)
            with self.assertRaisesRegex(cost_service.QuoteError,'连接暂不可用'):
                cost_service.run_query('ticket',{'scenic_name':'B'},wait_for_slot=True)
            self.assertEqual(run.call_count,1)
            record,_=cost_service.run_query('hotel',{'cityName':'北京'},wait_for_slot=True)
            self.assertEqual(record['rows'],[])
            self.assertEqual(run.call_count,2)
            cache.delete(cost_service._connection_key('ticket'))
            with patch('travel.cost_service.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"tickets":[]}','')) as retry:
                cost_service.run_query('ticket',{'scenic_name':'B'},wait_for_slot=True)
                retry.assert_called_once()

    def test_format_failure_does_not_pause_other_queries(self):
        bad=subprocess.CompletedProcess([],0,'unrecognised response','')
        good=subprocess.CompletedProcess([],0,'{"tickets":[]}','')
        with patch.dict(os.environ,{'TUNIU_API_KEY':'TEST_FORMAT_KEY'}), patch('travel.cost_service.cli_command',return_value=['node','test.js']), patch('travel.cost_service._last_call',0), patch('travel.cost_service.time.sleep'), patch('travel.cost_service.subprocess.run',side_effect=[bad,good]) as run:
            with self.assertRaises(cost_service.QuoteError):
                cost_service.run_query('ticket',{'scenic_name':'A'},wait_for_slot=True)
            record,_=cost_service.run_query('ticket',{'scenic_name':'B'},wait_for_slot=True)
            self.assertEqual(record['rows'],[])
            self.assertEqual(run.call_count,2)
