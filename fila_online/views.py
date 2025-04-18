from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Fila, Ticket
from .serializers import FilaSerializer, TicketSerializer
from django.utils import timezone
import uuid

class ListarFilas(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        filas = Fila.objects.all()
        serializer = FilaSerializer(filas, many=True)
        return Response(serializer.data)

class DetalheFila(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            fila = Fila.objects.get(pk=pk)
            serializer = FilaSerializer(fila)
            return Response(serializer.data)
        except Fila.DoesNotExist:
            return Response({"error": "Fila não encontrada"}, status=404)

class EmitirTicket(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            fila = Fila.objects.get(pk=pk)
            if fila.tickets_ativos >= fila.limite_diario:
                return Response({"error": "Limite diário atingido"}, status=400)
            
            numero_ticket = fila.ticket_atual + 1
            ticket = Ticket.objects.create(
                fila=fila,
                usuario=request.user,
                numero_ticket=numero_ticket,
                codigo_qr=str(uuid.uuid4()),
                prioridade=0,
                e_fisico=False,
                status='Pendente',
                emitido_em=timezone.now()
            )
            fila.ticket_atual = numero_ticket
            fila.tickets_ativos += 1
            fila.save()
            
            serializer = TicketSerializer(ticket)
            return Response(serializer.data, status=201)
        except Fila.DoesNotExist:
            return Response({"error": "Fila não encontrada"}, status=404)

class ListarTickets(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tickets = Ticket.objects.filter(usuario=request.user)
        serializer = TicketSerializer(tickets, many=True)
        return Response(serializer.data)