from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Instituicao, Filial, Categoria
from .serializers import InstituicaoSerializer, FilialSerializer, CategoriaSerializer

class ListarInstituicoes(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        instituicoes = Instituicao.objects.all()
        serializer = InstituicaoSerializer(instituicoes, many=True)
        return Response(serializer.data)

class ListarFiliais(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        filiais = Filial.objects.all()
        serializer = FilialSerializer(filiais, many=True)
        return Response(serializer.data)

class ListarCategorias(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        categorias = Categoria.objects.all()
        serializer = CategoriaSerializer(categorias, many=True)
        return Response(serializer.data)