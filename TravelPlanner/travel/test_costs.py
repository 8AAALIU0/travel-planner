from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import json
import os
import subprocess
from django.test import TestCase
from django.utils import timezone
from .cost_forms import CostBudgetForm, QuoteForm
from .cost_service import decode_result, normalize, QuoteError, run_query, load_saved_ticket
from .cost_views import budget_totals, BUDGET, QUOTE
from .planner_views import INPUT, RESULT
from .test_planner import preferences, rows
from .planner_service import build_plan


def day(): return (timezone.localdate()+timedelta(days=1)).isoformat()


def ticket(name='二球观光成人票', price='209'):
    return {'resName':name,'startPrice':price,'startDate':day(),'endDate':day(),'departsDate':day()}


class CostsTests(TestCase):
    def test_existing_pages_and_new_links(self):
        self.assertRedirects(self.client.get('/'), '/journey/', fetch_redirect_response=False)
        for url in ('/explore/','/plan/','/costs/'):
            r=self.client.get(url)
            self.assertEqual(r.status_code,200)
            self.assertContains(r,'费用查询与预算')
        self.assertEqual(self.client.get('/costs/export/').status_code,404)

    def test_strict_error_wrapper(self):
        for obj in ({'success':False,'result':{'tickets':[]}}, {'result':{'isError':True,'content':[{'type':'text','text':'{"tickets":[]}'}]}}, {'successCode':False,'data':[]}):
            with self.assertRaises(QuoteError): decode_result(json.dumps(obj))
        obj={'success':True,'result':{'content':[{'type':'text','text':'{"tickets":[]}'}]}}
        self.assertEqual(decode_result(json.dumps(obj)),{'tickets':[]})

    def test_ticket_boat_foreign_and_family_not_adopted(self):
        source=[ticket(),ticket('黄浦江游船成人票','95'),ticket('二球观光成人票(非中国大陆籍)'),ticket('二球观光家庭票2大1小'),ticket('二球观光成人票','0')]
        result=normalize('ticket',{'tickets':source},{'depart_date':day()})
        self.assertEqual([r['usable'] for r in result],[True,False,False,False,True])
        self.assertEqual(result[-1]['unit'],'0.00')
        source[0]['endDate']='2015-12-31'
        self.assertFalse(normalize('ticket',{'tickets':source[:1]},{'depart_date':day()})[0]['usable'])

    def test_train_price_not_lowest_sold_out(self):
        t={'trainNum':'G1','departureTime':day()+' 08:00','price':{'edzPrice':'150','swzPrice':'500'},'seatAvailable':{'edzNum':0,'swzNum':9}}
        out=normalize('train',{'data':[t]},{'departureDate':day()})
        self.assertEqual([r['usable'] for r in out],[False,True])
        t['departureTime']='2025-01-01 08:00'
        self.assertFalse(any(r['usable'] for r in normalize('train',{'data':[t]},{'departureDate':day()})))

    def test_flight_tax_missing_not_zero(self):
        flight={'flightNumber':'MU1','basePrice':'600','totalTax':'50','remainingSeats':'9','departureTime':day()+' 08:00'}
        result=normalize('flight',{'data':[flight]},{'departureDate':day()})
        self.assertEqual(result[0]['unit'],'650.00')
        del flight['totalTax']
        result=normalize('flight',{'data':[flight]},{'departureDate':day()})
        self.assertEqual(result[0]['unit'],'')
        self.assertFalse(result[0]['usable'])

    def test_hotel_lowest_never_automatically_multiplied(self):
        record={'kind':'hotel','args':{},'rows':normalize('hotel',{'hotels':[{'hotelName':'测试酒店','lowestPrice':300}]},{}),'queried_at':day()}
        session=self.client.session;session[QUOTE]=record;session.save()
        self.client.post('/costs/',{'action':'adopt','row':0,'confirm':'yes','target':'hotel'})
        self.assertNotIn(BUDGET,self.client.session)

    def test_budget_room_nights_and_roundtrip(self):
        values=dict(hotel=Decimal(300),hotel_total=None,rooms=2,nights=4,people=3,days=3,food=Decimal(80),transport=Decimal(30),outbound=Decimal(100),inbound=Decimal(200),budget=Decimal(5000))
        totals=budget_totals(values)
        self.assertEqual(totals['hotel'],2400)
        self.assertEqual(totals['outbound']+totals['inbound'],900)
        values['hotel_total']=Decimal(1800)
        self.assertEqual(budget_totals(values)['hotel'],1800)
        values['hotel_total']=Decimal(0)
        self.assertEqual(budget_totals(values)['hotel'],0)

    def test_budget_bridge_and_no_network_on_get(self):
        data=dict(action='budget',city='杭州',start_date=day(),days=3,people=2,budget='5000',rooms=1,nights=4,hotel='300',hotel_total='1100',outbound='100',inbound='200',food='80',transport='30',unknown_ticket='50')
        r=self.client.post('/costs/',data)
        self.assertRedirects(r,'/plan/')
        state=self.client.session[INPUT]
        self.assertEqual(state['intercity'],'600')
        self.assertEqual(state['hotel_total'],'1100')
        self.assertEqual(state['nights'],4)
        with patch('travel.cost_views.run_query') as api:
            self.client.get('/costs/');api.assert_not_called()

    def test_post_query_prg_and_export(self):
        record={'kind':'ticket','args':{'scenic_name':'东方明珠','depart_date':day()},'rows':normalize('ticket',{'tickets':[ticket()]},{'depart_date':day()}),'queried_at':day(),'pages':1}
        data=dict(action='query',kind='ticket',city='上海',day=day(),keyword='东方明珠',adults=2)
        with patch('travel.cost_views.run_query',return_value=(record,False)) as api:
            r=self.client.post('/costs/',data)
            self.assertRedirects(r,'/costs/')
            self.client.get('/costs/');self.assertEqual(api.call_count,1)
        export=self.client.get('/costs/export/')
        self.assertEqual(export.status_code,200)
        self.assertEqual(json.loads(export.content)['args']['depart_date'],day())

    def test_cli_quoting_key_and_cache(self):
        response=json.dumps({'success':True,'result':{'content':[{'type':'text','text':'{"tickets":[]}'}]}})
        def runner(args,**kw):
            self.assertEqual(json.loads(args[-1]),{'scenic_name':'东方明珠','depart_date':day()})
            self.assertFalse(kw['shell']);self.assertEqual(kw['env']['TUNIU_API_KEY'],'TEST_NOT_A_REAL_KEY')
            return subprocess.CompletedProcess(args,0,response,'')
        with patch.dict(os.environ,{'TUNIU_API_KEY':'TEST_NOT_A_REAL_KEY'}),patch('travel.cost_service.cli_command',return_value=['node','tuniu.js']),patch('travel.cost_service._last_call',0),patch('travel.cost_service.subprocess.run',side_effect=runner) as runner_mock:
            record,cached=run_query('ticket',{'scenic_name':'东方明珠','depart_date':day()})
            self.assertFalse(cached)
            self.assertNotIn('TEST_NOT_A_REAL_KEY',json.dumps(record))
            record,cached=run_query('ticket',{'scenic_name':'东方明珠','depart_date':day()})
            self.assertTrue(cached);self.assertEqual(runner_mock.call_count,1)

    def test_actual_saved_49_ticket_fixture(self):
        import tempfile, shutil
        from pathlib import Path
        from django.test import override_settings
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'ticket_results';target.mkdir()
            shutil.copyfile(Path(__file__).parent/'fixtures/ticket_reference_test.json',target/'tuniu_20261028_133600_110137.json')
            with override_settings(BASE_DIR=Path(folder)):
                record=load_saved_ticket('tuniu_20261028_133600_110137.json')
        self.assertEqual(len(record['rows']),49)
        self.assertEqual(record['args']['scenic_name'],'东方明珠')
        with self.assertRaises(QuoteError):load_saved_ticket('../db.sqlite3')

    def test_ticket_reference_only_exact_name_and_date(self):
        p=preferences(ticket_refs=[{'name':'示例公园0','date':day(),'unit':'200','label':'成人观光票','priceday':day(),'queried_at':timezone.now().isoformat()}])
        p['budget']=Decimal(10000)
        with patch('travel.planner_service.fetch_pool',return_value=(rows(1),[],0)):
            result=build_plan(p,'')
        stop=result['days'][0]['stops'][0]
        self.assertEqual(Decimal(stop['ticket_per_person']),200)
        self.assertFalse(stop['price_reference'])
        p['ticket_refs'][0]['date']='2025-01-01'
        with patch('travel.planner_service.fetch_pool',return_value=(rows(1),[],0)):
            result=build_plan(p,'')
        self.assertEqual(Decimal(result['days'][0]['stops'][0]['ticket_per_person']),50)

    def test_planner_respects_explicit_hotel_total_zero_and_nights(self):
        with patch('travel.planner_service.fetch_pool',return_value=(rows(1),[],0)):
            result=build_plan(preferences(nights=4),'')
        self.assertEqual(Decimal(result['costs']['hotel']),1200)
        with patch('travel.planner_service.fetch_pool',return_value=(rows(1),[],0)):
            result=build_plan(preferences(nights=4,hotel_total=Decimal(0)),'')
        self.assertEqual(Decimal(result['costs']['hotel']),0)

    def test_csrf_and_query_validation(self):
        from django.test import Client
        self.assertEqual(Client(enforce_csrf_checks=True).post('/costs/',{'action':'query'}).status_code,403)
        f=QuoteForm(dict(kind='hotel',city='上海',day=day(),end=day(),adults=2))
        self.assertFalse(f.is_valid())
