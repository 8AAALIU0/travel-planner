"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
]

# The public homepage opens the complete journey planner.
urlpatterns.insert(0, path("", RedirectView.as_view(pattern_name="travel_journey", permanent=False), name="site_home"))

# TRAVEL_DATA_V2
from travel.views import detail
urlpatterns.insert(0, path("attractions/<int:pk>/", detail, name="attraction_detail"))


# TRAVEL_COLLECT_V4
from travel.collect_views import catalog, collect
urlpatterns.insert(0, path("explore/", catalog, name="travel_home"))
urlpatterns.append(path("collect/", collect, name="travel_collect"))


# TRAVEL_PLANNER_V5
from travel.planner_views import planner, export_plan
urlpatterns += [path("plan/", planner, name="travel_planner"), path("plan/export/", export_plan, name="travel_plan_export")]

# TRAVEL_COSTS_V6
from travel.cost_views import costs, export_quote
urlpatterns += [path("costs/", costs, name="travel_costs"), path("costs/export/", export_quote, name="travel_quote_export")]

# TRAVEL_JOURNEY_V7
from travel.journey_views import journey, journey_step, journey_reset, journey_export
urlpatterns += [path("journey/", journey, name="travel_journey"),path("journey/step/", journey_step,name="travel_journey_step"),path("journey/reset/",journey_reset,name="travel_journey_reset"),path("journey/export/",journey_export,name="travel_journey_export")]

# TRAVEL_ROUTES_V8
from travel.journey_views import retry_transport, route_detail, route_map
urlpatterns += [path("journey/retry-transport/",retry_transport,name="travel_retry_transport"),path("journey/route/<int:day>/<int:stop>/",route_detail,name="travel_route_detail"),path("journey/route/<int:day>/<int:stop>/map/",route_map,name="travel_route_map")]

# TRAVEL_OPTIMIZER_V9
from travel.journey_views import optimize_existing
urlpatterns += [path("journey/optimize/",optimize_existing,name="travel_optimize_existing")]

# TRAVEL_TRANSPORT_V10
from travel.journey_views import transport_preferences,select_transport
urlpatterns += [path("journey/transport-preferences/",transport_preferences,name="travel_transport_preferences"),path("journey/select-transport/",select_transport,name="travel_select_transport")]

from travel.journey_views import retry_hotels
urlpatterns.append(path('journey/retry-hotels/',retry_hotels,name='travel_retry_hotels'))

# Scenic photos are independent of all quote/planning routes.
from travel.photo_views import scenic_photo
urlpatterns.append(path('journey/photos/<int:day>/<int:stop>/',scenic_photo,name='travel_scenic_photo'))
