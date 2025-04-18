from django.urls import path
from . import views

app_name = 'fila_online'

urlpatterns = [
    path('filas/', views.ListarFilas.as_view(), name='listar_filas'),
    path('filas/<uuid:pk>/', views.DetalheFila.as_view(), name='detalhe_fila'),
    path('filas/<uuid:pk>/emitir_ticket/', views.EmitirTicket.as_view(), name='emitir_ticket'),
    path('tickets/', views.ListarTickets.as_view(), name='listar_tickets'),
]