from unittest.mock import patch
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from .test_stay_budget import p
from .stay_policy import find_hotels,search_args
from .cost_service import QuoteError
from .journey_service import initial_job,advance
from .journey_forms import JourneyForm
from .test_journey import data,simple_plan
from .models import PlannerCache
from .journey_views import JOB


def reply(items):
    return {'rows':[{'name':name,'unit':price,'raw':{'hotelId':name}} for name,price in items],'queried_at':timezone.now().isoformat(),'data':{}}


class HotelFixTests(TestCase):
    def search(self,responses):
        q=p(city='北京市',people=4,hotel=Decimal(300))
        lodging={'checkIn':q['start_date'].isoformat(),'checkOut':(q['start_date']+timedelta(days=2)).isoformat()}
        with patch('travel.stay_policy.assess',side_effect=lambda r,*a:{**r,'commute_minutes':None}),patch('travel.journey_service.query',side_effect=responses) as api:
            result,report=find_hotels(q,lodging,None,'',api)
        return result,report,api

    def test_city_and_single_night_and_per_room_occupancy(self):
        result,report,api=self.search([reply([('预算酒店','250')])])
        args=api.call_args.args[1]
        self.assertEqual(args['cityName'],'北京');self.assertEqual(args['adultNum'],2)
        self.assertEqual((__import__('datetime').date.fromisoformat(args['checkOut'])-__import__('datetime').date.fromisoformat(args['checkIn'])).days,1)
        self.assertFalse(result[0]['over_budget']);self.assertEqual(report['reason'],'within_budget')

    def test_filtered_results_empty_fallback_uses_same_cap(self):
        result,report,api=self.search([reply([]),reply([('便宜酒店','260')])])
        self.assertEqual(api.call_count,2);self.assertNotIn('prices',api.call_args.args[1])
        self.assertEqual(report['eligible'],1);self.assertFalse(result[0]['over_budget'])

    def test_over_budget_quotes_visible_but_not_recommended_as_affordable(self):
        result,report,api=self.search([reply([('贵酒店','600')]),reply([('较近预算','350')])])
        self.assertEqual(report['eligible'],0);self.assertEqual(report['reason'],'above_budget')
        self.assertEqual(result[0]['unit'],'350');self.assertTrue(all(r['over_budget'] for r in result))
        self.assertEqual(Decimal(result[0]['over_by']),Decimal(50))

    def test_missing_prices_not_reported_as_absent_hotels(self):
        result,report,api=self.search([reply([('价格缺失','')]),reply([])])
        self.assertEqual(report['reason'],'missing_prices');self.assertFalse(result)

    def test_permission_failure_no_repeat_or_fabricated_hotels(self):
        result,report,api=self.search([QuoteError('酒店服务权限未开通')])
        self.assertEqual(api.call_count,1);self.assertEqual(report['reason'],'api_error');self.assertFalse(result)
        self.assertIn('权限',report['errors'][0])

    def test_retry_only_hotels_keeps_transport_and_ticket_refs(self):
        f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data)
        refs=[{'name':'原景点','date':data()['start_date'],'unit':'100'}]
        job.update(done=True,index=7,ticket_refs=refs,prices={},ticket_options=[{'name':'原景点'}])
        item=PlannerCache.objects.create(cache_key='c'*64,payload=job,expires_at=timezone.now()+timedelta(hours=1))
        session=self.client.session;session[JOB]=item.cache_key;session.save()
        self.client.post('/journey/retry-hotels/')
        item.refresh_from_db();job=item.payload
        with patch('travel.journey_service.build_plan',side_effect=simple_plan),patch('travel.journey_service.query',return_value=reply([('酒店','200')])) as api:
            job=advance(job,'');self.assertEqual(job['index'],6)
            self.assertEqual(job['ticket_refs'],refs);self.assertEqual(job['ticket_options'],[{'name':'原景点'}])
            self.assertTrue(all(c.args[0]=='hotel' for c in api.call_args_list))
            job=advance(job,'');self.assertTrue(job['done'])
