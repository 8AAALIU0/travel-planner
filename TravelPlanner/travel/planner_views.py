import json
import os
import time
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods, require_GET
from .planner_forms import PlannerForm
from .planner_service import build_plan

RESULT = "travel_plan_result_v5"
INPUT = "travel_plan_input_v5"


@require_http_methods(["GET", "POST"])
def planner(request):
    result = request.session.get(RESULT)
    if request.method == "POST":
        form = PlannerForm(request.POST)
        if form.is_valid():
            now = time.time()
            if now - request.session.get("travel_plan_last_v5", 0) < 10:
                messages.info(request, "请稍等10秒再生成，避免重复查询；修改条件后可重新规划。")
                return redirect("travel_planner")
            request.session["travel_plan_last_v5"] = now
            values = form.cleaned_data
            values["ticket_refs"] = request.session.get("travel_ticket_refs_v6", [])
            result = build_plan(values, os.environ.get("TRAVEL_AMAP_KEY", ""))
            request.session[RESULT] = result
            request.session[INPUT] = {k: v if v is None or isinstance(v, (int,list)) else str(v) for k,v in values.items() if k != "ticket_refs"}
            return redirect("travel_planner")
    else:
        form = PlannerForm(initial=request.session.get(INPUT, {}))
    return render(request, "travel/planner.html", {"form":form,"plan":result,"key_ready":bool(os.environ.get("TRAVEL_AMAP_KEY"))})


@require_GET
def export_plan(request):
    result = request.session.get(RESULT)
    if not result:
        return HttpResponse("请先生成一份旅行方案。",status=404)
    response = HttpResponse(json.dumps(result,ensure_ascii=False,indent=2),content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="travel_plan.json"'
    return response
