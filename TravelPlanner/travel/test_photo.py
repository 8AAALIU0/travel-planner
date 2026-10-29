import base64
import copy
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .models import PlannerCache
from .photo_service import _verified,_picture,_commons,_amap,_fetch,resolve_photo,photo_key
from .journey_views import JOB

class PhotoTests(TestCase):
    def setUp(self):
        self.stop={'name':'故宫博物院','poi_id':'B000A8UIN8','longitude':116.397,'latitude':39.918,'photos':[]}
        self.entity={'labels':{'zh':{'value':'故宫博物院'}},'claims':{'P625':[{'mainsnak':{'datavalue':{'value':{'longitude':116.397,'latitude':39.918}}}}]}}
    def test_reject_wrong_name_and_distant_same_name(self):
        self.assertTrue(_verified(self.entity,self.stop))
        self.assertFalse(_verified(self.entity,dict(self.stop,name='故宫角楼')))
        self.assertFalse(_verified(self.entity,dict(self.stop,longitude=121.0)))
        self.assertFalse(_verified(self.entity,dict(self.stop,latitude=None)))
    def test_private_or_arbitrary_image_urls_never_fetched(self):
        for url in ['http://127.0.0.1/secret','https://example.com/a.jpg','https://upload.wikimedia.org@localhost/a','file:///etc/passwd']:
            with self.assertRaises(ValueError):_fetch(url)
        with patch('travel.photo_service.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):_fetch('https://upload.wikimedia.org/a.jpg')
    @patch('travel.photo_service._fetch',return_value=b'<html>login</html>')
    def test_non_image_response_never_displayed(self,m):self.assertIsNone(_picture('https://upload.wikimedia.org/a','Commons'))
    @patch('travel.photo_service._commons')
    @patch('travel.photo_service._amap',return_value=None)
    def test_cache_avoids_repeated_calls_and_plan_stays_unchanged(self,amap,commons):
        commons.return_value={'data':base64.b64encode(b'\xff\xd8\xfftest').decode(),'mime':'image/jpeg','source':'Commons'}
        before=copy.deepcopy(self.stop);one=resolve_photo(self.stop);two=resolve_photo(self.stop)
        self.assertEqual(one,two);self.assertEqual(before,self.stop);commons.assert_called_once();amap.assert_called_once()
    @patch('travel.photo_service._commons',return_value=None)
    @patch('travel.photo_service._amap',return_value=None)
    def test_no_result_is_cached_without_other_landmark_picture(self,a,c):
        self.assertTrue(resolve_photo(self.stop)['missing']);self.assertTrue(resolve_photo(self.stop)['missing']);a.assert_called_once()
    def test_endpoint_requires_current_session_and_key(self):
        url='/journey/photos/0/0/?key='+photo_key(self.stop)
        with patch('travel.photo_views.resolve_photo') as service:
            self.assertEqual(self.client.get(url).status_code,404);service.assert_not_called()
            job={'done':True,'plan':{'days':[{'stops':[self.stop]}]}}
            item=PlannerCache.objects.create(cache_key='a'*64,payload=job,expires_at=timezone.now()+timedelta(hours=1))
            session=self.client.session;session[JOB]=item.cache_key;session.save()
            self.assertEqual(self.client.get('/journey/photos/0/0/?key=old').status_code,409);service.assert_not_called()
            service.return_value={'data':base64.b64encode(b'test').decode(),'mime':'image/jpeg','source':'Test'}
            response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertTrue(response.json()['url'].startswith('data:image/jpeg;base64,'))
            self.assertEqual(response['Cache-Control'],'private, no-store')
            item.refresh_from_db();self.assertEqual(item.payload,job)
    @patch('travel.photo_service._picture')
    @patch('travel.photo_service._json')
    def test_commons_uses_verified_entity_and_license_credit(self,api,picture):
        self.entity['claims']['P18']=[{'mainsnak':{'datavalue':{'value':'Palace.jpg'}}}]
        api.side_effect=[{'search':[{'id':'Q1'}]},{'entities':{'Q1':self.entity}},{'query':{'pages':{'1':{'imageinfo':[{'thumburl':'https://thumb.wikimedia.org/p.jpg','descriptionurl':'https://commons.wikimedia.org/wiki/File:Palace.jpg','extmetadata':{'Artist':{'value':'<a>Author</a>'},'LicenseShortName':{'value':'CC BY-SA 4.0'},'LicenseUrl':{'value':'https://creativecommons.org/licenses/by-sa/4.0/'}}}]}}}}]
        picture.return_value={'data':'abc','source':'Commons'}
        self.assertEqual(_commons(self.stop)['data'],'abc');kwargs=picture.call_args.kwargs;self.assertEqual(kwargs['author'],'Author');self.assertEqual(kwargs['license'],'CC BY-SA 4.0')
    @patch('travel.photo_service._picture')
    @patch('travel.photo_service._json')
    @patch.dict('os.environ',{'TRAVEL_AMAP_KEY':'not-a-real-key'})
    def test_amap_details_must_match_poi_id(self,api,picture):
        api.return_value={'status':'1','pois':[{'id':'OTHER','photos':[{'url':'https://store.is.autonavi.com/a.jpg'}]}]}
        self.assertIsNone(_amap(self.stop));picture.assert_not_called()
