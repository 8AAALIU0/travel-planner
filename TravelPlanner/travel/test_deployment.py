from unittest.mock import patch
from django.test import TestCase,Client,override_settings
from django.conf import settings
from django.core.cache import cache
from .journey_views import JOB
from .test_journey import data
from .deployment_gate import _single_query

@override_settings(MIDDLEWARE=settings.MIDDLEWARE+['travel.deployment_gate.DeploymentGate'])
class DeploymentTests(TestCase):
    def setUp(self):cache.clear()
    def test_visitors_get_independent_plans(self):
        a,b=Client(),Client()
        self.assertEqual(a.post('/journey/',data()).status_code,302)
        self.assertEqual(b.post('/journey/',data()).status_code,302)
        self.assertNotEqual(a.session[JOB],b.session[JOB])
        self.assertNotIn(JOB,self.client.session)
    def test_busy_step_polls_without_starting_api_or_mutating_job(self):
        _single_query.acquire()
        try:
            with patch('travel.journey_views.advance') as work:
                response=self.client.post('/journey/step/',{'index':0})
            self.assertEqual(response.status_code,200);self.assertTrue(response.json()['queued']);work.assert_not_called()
        finally:_single_query.release()
    @override_settings(TRAVEL_SESSION_STARTS_PER_HOUR=1)
    def test_repeat_plan_start_is_limited(self):
        self.client.post('/journey/',data());first=self.client.session[JOB]
        self.client.post('/journey/',data());self.assertEqual(self.client.session[JOB],first)
        response=self.client.get('/journey/');self.assertContains(response,'查询次数已用完')
    def test_legacy_manual_import_is_not_public(self):
        self.assertEqual(self.client.get('/collect/').status_code,403)
        self.assertEqual(self.client.post('/costs/',{}).status_code,403)
    def test_failed_view_releases_query_slot(self):
        self.client.post('/journey/',{})
        self.assertTrue(_single_query.acquire(blocking=False));_single_query.release()
    def test_travel_pages_have_private_cache_control(self):
        self.assertEqual(self.client.get('/journey/')['Cache-Control'],'private, no-store')
