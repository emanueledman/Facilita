from django.shortcuts import render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response

class ListarInstituicoes(APIView):
    def get(self, request):
        return Response({"message": "Lista de instituições"})

class ListarFiliais(APIView):
    def get(self, request):
        return Response({"message": "Lista de filiais"})

class ListarCategorias(APIView):
    def get(self, request):
        return Response({"message": "Lista de categorias"})