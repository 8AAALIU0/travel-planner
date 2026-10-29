from datetime import timedelta
from django import forms
from django.utils import timezone

INTERESTS = [("nature", "自然风光"), ("culture", "历史文化"), ("museum", "博物馆"), ("city", "城市漫步")]


class PlannerForm(forms.Form):
    city = forms.CharField(label="目的地城市", max_length=60, initial="杭州市")
    start_date = forms.DateField(label="开始日期", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    days = forms.IntegerField(label="游玩天数", min_value=1, max_value=5, initial=3)
    people = forms.IntegerField(label="人数（按成人估算）", min_value=1, max_value=10, initial=2)
    budget = forms.DecimalField(label="全体总预算（元）", min_value=1, max_value=1000000, decimal_places=2, max_digits=9, initial=2000)
    interests = forms.MultipleChoiceField(label="旅行兴趣", choices=INTERESTS, initial=["nature", "culture"], widget=forms.CheckboxSelectMultiple)
    pace = forms.ChoiceField(label="游玩节奏", choices=[("relaxed", "轻松 · 每日上限2个地点"), ("normal", "适中 · 每日上限3个地点")], initial="relaxed")
    mode = forms.ChoiceField(label="景点间交通", choices=[("transit", "公共交通"), ("walking", "步行"), ("driving", "驾车")], initial="transit")
    hotel = forms.DecimalField(label="每间房每晚预算（元）", min_value=0, max_value=10000, decimal_places=2, max_digits=7, initial=300)
    nights = forms.IntegerField(label="实际住宿晚数（留空按游玩天数减1）", required=False, min_value=0, max_value=30)
    hotel_total = forms.DecimalField(label="全体住宿总预算（可选，优先使用）", required=False, min_value=0, max_value=1000000, decimal_places=2, max_digits=9)
    rooms = forms.IntegerField(label="房间数", min_value=1, max_value=10, initial=1)
    food = forms.DecimalField(label="每人每天餐饮预算（元）", min_value=0, max_value=3000, decimal_places=2, max_digits=6, initial=80)
    transport = forms.DecimalField(label="每人每天市内交通预留（元）", min_value=0, max_value=3000, decimal_places=2, max_digits=6, initial=30)
    unknown_ticket = forms.DecimalField(label="未知票价每人每景点预留（元）", min_value=1, max_value=3000, decimal_places=2, max_digits=6, initial=50)
    intercity = forms.DecimalField(label="全体往返目的地交通预留（元）", min_value=0, max_value=1000000, decimal_places=2, max_digits=9, initial=0)

    def clean_city(self):
        city = self.cleaned_data["city"].strip()
        if city in ("中国", "全国", "全部") or not city:
            raise forms.ValidationError("请填写具体城市，例如杭州市。")
        return city

    def clean_interests(self):
        interests = self.cleaned_data["interests"]
        if len(interests) > 3:
            raise forms.ValidationError("一次最多选择3类兴趣。")
        return interests

    def clean(self):
        data = super().clean()
        people, rooms, hotel = data.get("people"), data.get("rooms"), data.get("hotel")
        if people and rooms and hotel and rooms * 2 < people:
            self.add_error("rooms", "本版按每间房最多2名成人估算，请增加房间数。")
        return data

    def clean_start_date(self):
        day = self.cleaned_data["start_date"]
        today = timezone.localdate()
        if not today <= day <= today + timedelta(days=365):
            raise forms.ValidationError("请选择今天至未来一年内的日期。")
        return day
