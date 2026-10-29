from datetime import timedelta
from django import forms
from django.utils import timezone


class QuoteForm(forms.Form):
    kind = forms.ChoiceField(label='查询项目', choices=[('ticket','景点门票'),('hotel','酒店住宿'),('train','火车票'),('flight','国内机票')])
    city = forms.CharField(label='目的地城市', max_length=60, initial='上海')
    origin = forms.CharField(label='出发城市（火车/飞机必填）', max_length=60, required=False)
    day = forms.DateField(label='游玩 / 入住 / 出发日期', initial=timezone.localdate, widget=forms.DateInput(attrs={'type':'date'}))
    end = forms.DateField(label='离店日期（酒店必填）', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    keyword = forms.CharField(label='景点名称 / 酒店关键词', max_length=100, required=False)
    adults = forms.IntegerField(label='成人数', min_value=1, max_value=10, initial=2)

    def clean(self):
        d = super().clean()
        day, end, kind = d.get('day'), d.get('end'), d.get('kind')
        if day and not timezone.localdate() <= day <= timezone.localdate()+timedelta(days=365):
            self.add_error('day','请选择今天至未来一年内的日期。')
        if kind == 'ticket' and not d.get('keyword'):
            self.add_error('keyword','请填写完整景点名称。')
        if kind in ('train','flight'):
            if not d.get('origin'):
                self.add_error('origin','请填写出发城市。')
            elif d.get('origin') == d.get('city'):
                self.add_error('city','出发城市与到达城市不能相同。')
        if kind == 'hotel' and (not end or not day or not 1 <= (end-day).days <= 30):
            self.add_error('end','离店日期需晚于入住日期，最多连续30晚。')
        return d

    def arguments(self):
        d = self.cleaned_data
        if d['kind'] == 'ticket':
            return {'scenic_name': d['keyword'], 'depart_date':d['day'].isoformat()}
        if d['kind'] == 'hotel':
            args = {'cityName':d['city'],'checkIn':d['day'].isoformat(),'checkOut':d['end'].isoformat(),'adultNum':d['adults']}
            if d.get('keyword'): args['keyword'] = d['keyword']
            return args
        return {'departureCityName':d['origin'],'arrivalCityName':d['city'],'departureDate':d['day'].isoformat()}


class CostBudgetForm(forms.Form):
    city = forms.CharField(label='目的地城市',max_length=60,initial='上海')
    start_date = forms.DateField(label='第一天游玩日期',initial=timezone.localdate,widget=forms.DateInput(attrs={'type':'date'}))
    days = forms.IntegerField(label='游玩天数', min_value=1,max_value=5,initial=3)
    people = forms.IntegerField(label='成人数',min_value=1,max_value=10,initial=2)
    budget = forms.DecimalField(label='全体总预算（元）',min_value=1,max_value=1000000,max_digits=9,decimal_places=2,initial=5000)
    rooms = forms.IntegerField(label='房间数',min_value=1,max_value=10,initial=1)
    nights = forms.IntegerField(label='实际住宿晚数',min_value=0,max_value=30,initial=2)
    hotel = forms.DecimalField(label='每间每晚住宿估算（元）',min_value=0,max_value=10000,max_digits=7,decimal_places=2,initial=300)
    hotel_total = forms.DecimalField(label='全体住宿总额（可选，填写后替代每晚估算）',required=False,min_value=0,max_value=1000000,max_digits=9,decimal_places=2)
    outbound = forms.DecimalField(label='去程每位成人预算（元）',min_value=0,max_value=100000,max_digits=8,decimal_places=2,initial=0)
    inbound = forms.DecimalField(label='返程每位成人预算（元）',min_value=0,max_value=100000,max_digits=8,decimal_places=2,initial=0)
    food = forms.DecimalField(label='每人每天餐饮预算（元）',min_value=0,max_value=3000,max_digits=6,decimal_places=2,initial=80)
    transport = forms.DecimalField(label='每人每天市内交通预留（元）',min_value=0,max_value=3000,max_digits=6,decimal_places=2,initial=30)
    unknown_ticket = forms.DecimalField(label='未知票价每人每景点预留（元）',min_value=1,max_value=3000,max_digits=6,decimal_places=2,initial=50)

    def clean_start_date(self):
        day = self.cleaned_data['start_date']
        if not timezone.localdate() <= day <= timezone.localdate()+timedelta(days=365):
            raise forms.ValidationError('请选择今天至未来一年内的日期。')
        return day

    def clean(self):
        d = super().clean()
        if d.get('rooms') and d.get('people') and d['rooms']*2 < d['people']:
            self.add_error('rooms','本版按每间最多2名成人估算，请增加房间数。')
        return d
