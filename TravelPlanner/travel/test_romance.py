from html.parser import HTMLParser
from unittest.mock import patch
from django.test import TestCase

class Tags(HTMLParser):
    def __init__(self,content):
        super().__init__();self.rows=[];self.feed(content)
    def handle_starttag(self,tag,attrs):self.rows.append((tag,dict(attrs)))

class RomanceHomeTests(TestCase):
    def test_home_recommendations_never_query_or_submit_travel_services(self):
        with patch('travel.journey_service.query') as quote,patch('travel.photo_service.resolve_photo') as photos:
            response=self.client.get('/journey/')
        self.assertEqual(response.status_code,200);quote.assert_not_called();photos.assert_not_called()
        for name in ['桂林','杭州','大理','destination-next','motion-toggle','非实时热门榜','journey-form','生成整趟旅行方案','music-file']:
            self.assertContains(response,name)
        self.assertNotContains(response,'实时热门排行榜')
    def test_recommendation_buttons_do_not_submit_and_original_inputs_remain_unique(self):
        rows=Tags(self.client.get('/journey/').content.decode()).rows
        for tag,attrs in rows:
            if 'data-preset-city' in attrs:self.assertEqual(tag,'button');self.assertEqual(attrs.get('type'),'button')
        for field in ['id_origin','id_city','id_people','id_budget','id_keywords']:
            self.assertEqual(sum(a.get('id')==field for t,a in rows),1)
    def test_hero_images_are_local_and_attributed(self):
        content=self.client.get('/journey/').content.decode();rows=Tags(content).rows
        images=[a for t,a in rows if t=='img' and 'destination-photo' in a.get('class','')]
        self.assertEqual(len(images),3)
        for image in images:self.assertTrue(image['src'].startswith('/static/'))
        self.assertIn('CC BY-SA 4.0',content);self.assertIn('Takashishin',content)
