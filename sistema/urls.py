from django.urls import path
from . import views

app_name = 'sistema'

urlpatterns = [
    path('instituicoes/', views.ListarInstituicoes.as_view(), name='listar_instituicoes'),
    path('filiais/', views.ListarFiliais.as_view(), name='listar_filiais'),
    path('categorias/', views.ListarCategorias.as_view(), name='listar_categorias'),
]