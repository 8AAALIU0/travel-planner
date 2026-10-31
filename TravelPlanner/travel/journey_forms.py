import re
from datetime import timedelta
from django import forms
from django.utils import timezone
from .planner_forms import PlannerForm


class TransportPreferenceForm(forms.Form):
    travel_priority=forms.ChoiceField(label='这次出行更看重什么',required=False,choices=[('economy','经济优先 · 接受较长车程'),('balanced','均衡选择 · 兼顾花费与时间'),('fast','时间优先 · 尽量缩短在途时间')],initial='balanced')
    travel_kind=forms.ChoiceField(label='比较哪些交通工具',choices=[('train','火车 / 高铁'),('flight','飞机'),('compare','火车与飞机一起比较')],initial='train')
    allow_overnight=forms.BooleanField(label='接受跨夜抵达（按实际到达日排景点）',required=False,initial=True)
    seat_preference=forms.ChoiceField(label='火车座席偏好',required=False,choices=[('any','不限座席（不推荐无座）'),('ordinary','硬座 / 软座 / 卧铺'),('sleeper','只选卧铺'),('second','只选二等座')],initial='any')
    max_travel_hours=forms.IntegerField(label='单程在途时长上限（小时，可留空）',required=False,min_value=1,max_value=72)
    transport_budget_pct=forms.IntegerField(label='往返交通最多占总预算的百分比（可留空）',required=False,min_value=1,max_value=95)

    minimum_sights=forms.IntegerField(label='整趟至少希望游玩几个景点',required=False,min_value=1,max_value=30,initial=3,help_text='这是整趟旅行的目标数量，与每天最多几个不同；空白按最多3个计算。')
    minimum_play_days=forms.IntegerField(label='至少几天安排景点（留空为每天）',required=False,min_value=1,max_value=14,help_text='先保护这些游玩天数，再比较交通价格；找不到满足条件的组合会明确提示。')

    def clean_travel_priority(self):return self.cleaned_data.get('travel_priority') or 'balanced'
    def clean_seat_preference(self):return self.cleaned_data.get('seat_preference') or 'any'


class StayPreferenceForm(forms.Form):
    stay_kind=forms.ChoiceField(label='需要预订酒店吗',required=False,choices=[('hotel','需要酒店'),('free','不用酒店 · 住亲友家或已有免费住所')],initial='hotel')
    hotel_preference=forms.ChoiceField(label='住宿更看重什么',required=False,choices=[('economy','价格优先 · 接受较远位置'),('balanced','均衡 · 兼顾房价和通勤'),('convenient','交通优先 · 尽量靠近游玩地点')],initial='balanced')
    stay_area=forms.CharField(label='希望住在哪个地标附近（可留空）',required=False,max_length=60,help_text='例如地铁站或景点；不用填写亲友家的具体门牌。不填写时，交通便利度以行程首个景点为参照。')
    def clean_stay_kind(self):return self.cleaned_data.get('stay_kind') or 'hotel'
    def clean_hotel_preference(self):return self.cleaned_data.get('hotel_preference') or 'balanced'


class JourneyForm(PlannerForm,TransportPreferenceForm,StayPreferenceForm):
    origin = forms.CharField(label='从哪里出发', max_length=60, initial='上海')
    keywords = forms.CharField(label='想玩什么', max_length=100, initial='自然风光、历史文化',
                              widget=forms.TextInput(attrs={'placeholder':'例如：自然风光、博物馆、亲子；最多3项'}))
    ticket_policy=forms.ChoiceField(label='门票预算怎样计算',required=False,choices=[('auto','自动参考资料，未知景点按类别估算'),('manual','未知景点使用我填写的单人预留')],initial='auto')
    def clean_ticket_policy(self):return self.cleaned_data.get('ticket_policy') or 'auto'

    budget_strategy=forms.ChoiceField(label='剩余预算怎样使用',required=False,choices=[('experience','体验优先 · 相近品质下充分使用预算'),('quality','品质优先 · 同等体验少花钱')],initial='experience',help_text='先满足时间、预算和游玩目标；在不降低已找到的评分与目标达成程度时比较有来源票价的景点。未知估价不会被当作升级。')
    def clean_budget_strategy(self):return self.cleaned_data.get('budget_strategy') or 'experience'

    fallback_return = forms.DecimalField(label='未查到报价时，每位成人往返交通预留', min_value=1,max_value=100000,decimal_places=2,max_digits=8,initial=600)

    def __init__(self,*a,**kw):
        super().__init__(*a,**kw)
        for name in ['interests','intercity','hotel_total']:
            self.fields.pop(name)
        self.fields['unknown_ticket'].initial=150
        self.fields['unknown_ticket'].label='没有票价资料时，单人单景点最低预留（元）'
        self.fields['unknown_ticket'].help_text='自动估价模式会按景点类别提高预留；它不是所有景点的统一票价。'
        self.fields['hotel'].initial=200
        self.fields['hotel'].label='每间每晚最高预算（元）'
        self.fields['hotel'].help_text='用于查询价格区间及每间每晚预算；平台最低起价的计价单位未确认，不直接代替住宿总额。'
        self.fields['city'].label='想去哪个城市'
        self.fields['budget'].initial=5000
        self.fields['start_date'].initial=lambda:timezone.localdate()+timedelta(days=2)
        self.fields['days'].label='出发到返回，一共几天'
        self.order_fields(['origin','city','start_date','days','people','budget','budget_strategy','keywords','stay_kind','hotel_preference','stay_area','travel_kind','travel_priority','pace','mode','minimum_sights','minimum_play_days','allow_overnight','seat_preference','max_travel_hours','transport_budget_pct','rooms','nights','hotel','food','transport','unknown_ticket','fallback_return'])
        self.fields['hotel'].required=False
        self.fields['rooms'].required=False
        self.fields['nights'].help_text='留空按实际抵达、返程日期计算；填写后使用手动晚数，需核对交通日期。'

    def clean_keywords(self):
        words=list(dict.fromkeys(x for x in re.split(r'[\s,，、;；]+',self.cleaned_data['keywords'].strip()) if x))
        if not 1 <= len(words) <= 3:
            raise forms.ValidationError('请填写1至3个关键词，用逗号或顿号分开。')
        return words

    def clean(self):
        d=super().clean()
        if d.get('origin')==d.get('city'):
            self.add_error('origin','出发城市与目的地不同；本版面向跨城旅行。')
        if d.get('stay_kind')!='free':
            if d.get('hotel') is None:self.add_error('hotel','需要酒店时，请填写每间每晚预算。')
            if d.get('rooms') is None:self.add_error('rooms','需要酒店时，请填写房间数。')
        if d.get('stay_kind')=='free':
            d.update(hotel=0,rooms=1,nights=0)
            self._errors.pop('hotel',None);self._errors.pop('rooms',None);self._errors.pop('nights',None)
        if d.get('nights') is not None and d.get('days') and d['nights'] > d['days']-1:
            self.add_error('nights','本流程住宿从出发日开始，晚数不能超过旅行天数减1；请增加旅行天数或减少晚数。')
        return d
