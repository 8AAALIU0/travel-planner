import copy
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from .presentation import image_urls,brief,present_job
from .models import Attraction,AmapRecord,PlannerCache
from .journey_forms import JourneyForm
from .journey_service import initial_job
from .journey_views import JOB
from .test_journey import data,simple_plan


class VisualTests(TestCase):
    def test_image_urls_safe_and_deduplicated(self):
        values=['javascript:alert(1)','https://example.com/a.jpg','https://example.com/a.jpg',{'url':'https://example.com/b.jpg'},'https://user:password@example.com/a.jpg','http://127.0.0.1/test']
        self.assertEqual(image_urls(values),['https://example.com/a.jpg','https://example.com/b.jpg'])

    def test_presentation_never_mutates_saved_prices_or_plan(self):
        source={'prices':{'outbound':{'unit':'529'}},'plan':{'days':[{'stops':[{'name':'测试公园','category':'公园','photos':['https://example.com/photo.jpg'],'group_ticket':'300'}]}]},'hotels':[{'raw':{'firstPic':'https://example.com/hotel.jpg'}}]}
        original=copy.deepcopy(source);result=present_job(source)
        self.assertEqual(source,original)
        stop=result['plan']['days'][0]['stops'][0]
        self.assertEqual(stop['ui_photos'],['https://example.com/photo.jpg']);self.assertIn('慢下来',stop['ui_intro'])
        self.assertEqual(stop['group_ticket'],'300');self.assertEqual(result['prices'],source['prices'])
        self.assertEqual(result['hotels'][0]['ui_photo'],'https://example.com/hotel.jpg')

    def test_real_maintained_description_and_newer_database_photos_used(self):
        spot=Attraction.objects.create(name='照片测试公园',city='测试市',description='这是已维护的景点介绍。')
        AmapRecord.objects.create(attraction=spot,poi_id='PHOTO_V15',name=spot.name,city='测试市',photos=['https://example.com/live.jpg'],fetched_at=timezone.now())
        job={'plan':{'days':[{'stops':[{'name':spot.name,'poi_id':'PHOTO_V15','detail_pk':spot.pk,'photos':[]}]}]}}
        row=present_job(job)['plan']['days'][0]['stops'][0]
        self.assertEqual(row['ui_intro'],spot.description);self.assertEqual(row['ui_photos'],['https://example.com/live.jpg'])

    def test_no_picture_never_uses_random_other_attraction(self):
        job={'plan':{'days':[{'stops':[{'name':'无照片的公园','category':'公园'}]}]}}
        row=present_job(job)['plan']['days'][0]['stops'][0]
        self.assertFalse(row['ui_photos']);self.assertIn('地点类别',row['ui_intro_source'])

    def test_page_keeps_main_actions_and_shows_music_without_api_queries(self):
        with patch('travel.journey_service.query') as api:
            response=self.client.get('/journey/');api.assert_not_called()
        for text in ['值得出发','开启旅途配乐','生成整趟旅行方案','journey.js','music-file']:
            self.assertContains(response,text)
        self.assertNotContains(response,'upgrade_speed')

    def test_gallery_card_and_missing_picture_placeholder_render(self):
        f=JourneyForm(data());f.is_valid();job=initial_job(f.cleaned_data)
        plan=simple_plan(__import__('travel.journey_service',fromlist=['restore']).restore(job['input']),'')
        plan['days'][0]['stops']=[{'name':'有照片公园','photos':['https://example.com/photo.jpg'],'category':'公园'}, {'name':'缺图公园','category':'公园'}]
        job.update(done=True,index=7,plan=plan)
        item=PlannerCache.objects.create(cache_key='f'*64,payload=job,expires_at=timezone.now()+timedelta(hours=1))
        session=self.client.session;session[JOB]=item.cache_key;session.save()
        response=self.client.get('/journey/')
        self.assertContains(response,'data-gallery-open');self.assertContains(response,'正在寻找这一站的风景')
        self.assertContains(response,'按地点类别整理的简述')
        item.refresh_from_db();self.assertNotIn('ui_intro',item.payload['plan']['days'][0]['stops'][0])
