from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/painel/(?P<instituicao_id>[^/]+)/$', consumers.PainelConsumer.as_asgi()),
    re_path(r'ws/senhas/(?P<senha_id>[^/]+)/$', consumers.SenhaConsumer.as_asgi()),
]