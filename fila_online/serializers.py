from rest_framework import serializers
from .models import Fila, Ticket, Departamento
from sistema.serializers import FilialSerializer, CategoriaSerializer

class DepartamentoSerializer(serializers.ModelSerializer):
    filial = FilialSerializer(read_only=True)
    
    class Meta:
        model = Departamento
        fields = ['id', 'filial', 'nome', 'setor']

class FilaSerializer(serializers.ModelSerializer):
    departamento = DepartamentoSerializer(read_only=True)
    categoria = CategoriaSerializer(read_only=True)
    
    class Meta:
        model = Fila
        fields = ['id', 'departamento', 'servico', 'categoria', 'prefixo', 'hora_abertura', 'hora_fechamento', 'limite_diario', 'tickets_ativos', 'ticket_atual', 'tempo_espera_medio', 'num_balcoes']

class TicketSerializer(serializers.ModelSerializer):
    fila = FilaSerializer(read_only=True)
    
    class Meta:
        model = Ticket
        fields = ['id', 'fila', 'numero_ticket', 'codigo_qr', 'prioridade', 'e_fisico', 'status', 'emitido_em', 'expira_em', 'atendido_em', 'balcao']