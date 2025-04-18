from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
from django.core.exceptions import ValidationError

# Enums
class PapelUsuario(models.TextChoices):
    USUARIO = 'usuario', 'Usuário'
    ADMIN_DEPARTAMENTO = 'admin_departamento', 'Administrador de Departamento'
    ADMIN_INSTITUICAO = 'admin_instituicao', 'Administrador de Instituição'
    ADMIN_FILIAL = 'admin_filial', 'Administrador de Filial'

# Instituição
class Instituicao(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nome = models.CharField(max_length=100)
    descricao = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
        ]

    def __str__(self):
        return self.nome

# Filial
class Filial(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instituicao = models.ForeignKey(Instituicao, on_delete=models.CASCADE, related_name='filiais')
    nome = models.CharField(max_length=100)
    localizacao = models.CharField(max_length=200, null=True, blank=True)
    bairro = models.CharField(max_length=100, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['instituicao']),
            models.Index(fields=['bairro']),
        ]

    def __str__(self):
        return f"{self.nome} de {self.instituicao.nome}"

# Categoria
class Categoria(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nome = models.CharField(max_length=100)
    categoria_pai = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='subcategorias')
    descricao = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
        ]

    def __str__(self):
        return self.nome

# PerfilUsuario
class PerfilUsuario(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfil')
    token_fcm = models.CharField(max_length=255, null=True, blank=True)
    papel_usuario = models.CharField(max_length=20, choices=PapelUsuario.choices, default=PapelUsuario.USUARIO)
    instituicao = models.ForeignKey(Instituicao, null=True, blank=True, on_delete=models.SET_NULL, related_name='admins')
    filial = models.ForeignKey(Filial, null=True, blank=True, on_delete=models.SET_NULL, related_name='admins')
    ultima_latitude = models.FloatField(null=True, blank=True)
    ultima_longitude = models.FloatField(null=True, blank=True)
    ultima_atualizacao_local = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(default=timezone.now)
    ativo = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=['usuario']),
            models.Index(fields=['papel_usuario']),
            models.Index(fields=['instituicao']),
            models.Index(fields=['filial']),
        ]

    def clean(self):
        if User.objects.filter(email=self.usuario.email).exclude(id=self.usuario.id).exists():
            raise ValidationError("Email já está em uso.")

    def definir_senha(self, senha):
        if senha:
            self.usuario.set_password(senha)
            self.usuario.save()

    def verificar_senha(self, senha):
        if not senha:
            return False
        return self.usuario.check_password(senha)

    def __str__(self):
        return f"{self.usuario.email} ({self.papel_usuario})"

# PreferenciaUsuario
class PreferenciaUsuario(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='preferencias')
    instituicao = models.ForeignKey(Instituicao, null=True, blank=True, on_delete=models.SET_NULL, related_name='preferida_por')
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.SET_NULL, related_name='preferida_por')
    bairro = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['id']),
            models.Index(fields=['usuario'], name='idx_preferencia_usuario_id'),
            models.Index(fields=['instituicao']),
            models.Index(fields=['categoria']),
        ]

    def __str__(self):
        return f"Preferência de {self.usuario.email}"

# LogAuditoria
class LogAuditoria(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    id_usuario = models.CharField(max_length=36, null=True, blank=True)
    acao = models.CharField(max_length=255)
    tipo_recurso = models.CharField(max_length=255)
    id_recurso = models.CharField(max_length=36)
    detalhes = models.TextField(null=True, blank=True)
    data_hora = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=['data_hora'], name='idx_log_auditoria_data_hora'),
        ]

    def __str__(self):
        return f"{self.acao} em {self.tipo_recurso} {self.id_recurso}"