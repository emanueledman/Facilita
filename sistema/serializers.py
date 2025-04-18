from rest_framework import serializers
from .models import Instituicao, Filial, Categoria

class InstituicaoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instituicao
        fields = ['id', 'nome', 'descricao']

class FilialSerializer(serializers.ModelSerializer):
    instituicao = InstituicaoSerializer(read_only=True)

    class Meta:
        model = Filial
        fields = ['id', 'instituicao', 'nome', 'localizacao', 'bairro', 'latitude', 'longitude']

class CategoriaSerializer(serializers.ModelSerializer):
    categoria_pai = serializers.PrimaryKeyRelatedField(queryset=Categoria.objects.all(), allow_null=True)

    class Meta:
        model = Categoria
        fields = ['id', 'nome', 'categoria_pai', 'descricao']