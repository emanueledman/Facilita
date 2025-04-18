from django.contrib import admin
from django.urls import path, include
from rest_framework.views import APIView
from rest_framework.response import Response

class HealthCheck(APIView):
    def get(self, request):
        return Response({"status": "ok"})

urlpatterns = [
    path('', HealthCheck.as_view(), name='health_check'),
    path('admin/', admin.site.urls),
    path('api/sistema/', include('sistema.urls')),
    path('api/fila_online/', include('fila_online.urls')),
    path('accounts/', include('allauth.urls')),
]