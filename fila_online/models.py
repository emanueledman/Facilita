import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from sistema.models import Instituicao, Filial, Categoria, PerfilUsuario

# Enums
class DiaSemana(models.TextChoices):
    SEGUNDA = 'Segunda', 'Segunda-feira'
    TERCA = 'Terça', 'Terça-feira'
    QUARTA = 'Quarta', 'Quarta-feira'
    QUINTA = 'Quinta', 'Quinta-feira'
    SEXTA = 'Sexta', 'Sexta-feira'
    SABADO = 'Sábado', 'Sábado'
    DOMINGO = 'Domingo', 'Domingo'

# Departamento
class Departamento(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filial = models.ForeignKey(Filial, on_delete=models.CASCADE, related_name='departamentos')
    nome = models.CharField(max_length=50)
    setor = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['filial']),
            models.Index(fields=['nome']),
        ]

    def __str__(self):
        return f"{self.nome} na {self.filial.nome}"

# Fila
class Fila(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    departamento = models.ForeignKey(Departamento, on_delete=models.CASCADE, related_name='filas')
    servico = models.CharField(max_length=50)
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.SET_NULL, related_name='filas')
    prefixo = models.CharField(max_length=10)
    hora_abertura = models.TimeField()
    hora_fechamento = models.TimeField(null=True, blank=True)
    limite_diario = models.IntegerField()
    tickets_ativos = models.IntegerField(default=0)
    ticket_atual = models.IntegerField(default=0)
    tempo_espera_medio = models.FloatField(null=True, blank=True)
    ultimo_tempo_servico = models.FloatField(null=True, blank=True)
    num_balcoes = models.IntegerField(default=1)
    ultimo_balcao = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['departamento']),
            models.Index(fields=['servico']),
            models.Index(fields=['categoria']),
            models.Index(fields=['departamento', 'servico'], name='idx_fila_instituicao_servico'),
        ]

    def __str__(self):
        return f"{self.servico} no {self.departamento.nome}"

# Ticket
class Ticket(models.Model):
    STATUS_ESCOLHAS = [
        ('Pendente', 'Pendente'),
        ('Atendido', 'Atendido'),
        ('Cancelado', 'Cancelado'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fila = models.ForeignKey(Fila, on_delete=models.CASCADE, related_name='tickets')
    usuario = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='tickets')
    numero_ticket = models.IntegerField()
    codigo_qr = models.CharField(max_length=50, unique=True)
    prioridade = models.IntegerField(default=0)
    e_fisico = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_ESCOLHAS, default='Pendente')
    emitido_em = models.DateTimeField(default=timezone.now)
    expira_em = models.DateTimeField(null=True, blank=True)
    atendido_em = models.DateTimeField(null=True, blank=True)
    cancelado_em = models.DateTimeField(null=True, blank=True)
    balcao = models.IntegerField(null=True, blank=True)
    tempo_servico = models.FloatField(null=True, blank=True)
    dados_recibo = models.TextField(null=True, blank=True)
    troca_disponivel = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['fila']),
            models.Index(fields=['usuario']),
            models.Index(fields=['codigo_qr']),
        ]

    def __str__(self):
        return f"Ticket {self.numero_ticket} para Fila {self.fila_id}"

# HorarioFila
class HorarioFila(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fila = models.ForeignKey(Fila, on_delete=models.CASCADE, related_name='horarios')
    dia_semana = models.CharField(max_length=20, choices=DiaSemana.choices)
    hora_abertura = models.TimeField(null=True, blank=True)
    hora_fechamento = models.TimeField(null=True, blank=True)
    esta_fechado = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['fila'], name='idx_horario_fila_id'),
        ]

    def __str__(self):
        return f"Horário para {self.fila.servico} na {self.dia_semana}"

# EtiquetaServico
class EtiquetaServico(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    etiqueta = models.CharField(max_length=50)
    fila = models.ForeignKey(Fila, on_delete=models.CASCADE, related_name='etiquetas')

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['etiqueta']),
            models.Index(fields=['fila']),
        ]

    def __str__(self):
        return f"{self.etiqueta} para Fila {self.fila_id}"