from django.shortcuts import render

from rest_framework.views import APIView
from rest_framework.response import Response

class ListarFilas(APIView):
    def get(self, request):
        return Response({"message": "Lista de filas"})

class DetalheFila(APIView):
    def get(self, request, pk):
        return Response({"message": f"Detalhes da fila {pk}"})

class EmitirTicket(APIView):
    def post(self, request, pk):
        return Response({"message": f"Ticket emitido para fila {pk}"})

class ListarTickets(APIView):
    def get(self, request):
        return Response({"message": "Lista de tickets"})