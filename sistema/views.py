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
    
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from sistema.models import PerfilUsuario, Instituicao, Filial, PapelUsuario
from fila_online.models import Fila, Departamento
import logging

logger = logging.getLogger(__name__)

class AdminLoginView(APIView):
    def options(self, request, *args, **kwargs):
        return Response(
            headers={
                'Access-Control-Allow-Origin': request.headers.get('Origin', '*'),
                'Access-Control-Allow-Methods': 'POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                'Access-Control-Max-Age': '86400',
            },
            status=status.HTTP_200_OK
        )

    def post(self, request):
        logger.info("Recebida requisição POST para /api/admin/login")
        try:
            data = request.data
            email = data.get('email')
            password = data.get('password')

            if not email or not password:
                logger.warning("Tentativa de login sem email ou senha")
                return Response({"error": "Email e senha são obrigatórios"}, status=status.HTTP_400_BAD_REQUEST)

            user = authenticate(username=email, password=password)
            if not user:
                logger.warning(f"Credenciais inválidas para email={email}")
                return Response({"error": "Credenciais inválidas"}, status=status.HTTP_401_UNAUTHORIZED)

            perfil = PerfilUsuario.objects.filter(usuario=user).first()
            if not perfil:
                logger.warning(f"Perfil não encontrado para email={email}")
                return Response({"error": "Perfil de usuário não encontrado"}, status=status.HTTP_401_UNAUTHORIZED)

            if perfil.papel_usuario not in [PapelUsuario.ADMIN_DEPARTAMENTO, PapelUsuario.ADMIN_INSTITUICAO]:
                logger.warning(f"Usuário {email} tem papel inválido: {perfil.papel_usuario}")
                return Response(
                    {"error": "Acesso restrito a administradores de departamento ou instituição"},
                    status=status.HTTP_403_FORBIDDEN
                )

            token, created = Token.objects.get_or_create(user=user)
            response_data = {
                "token": token.key,
                "user_id": str(user.id),
                "user_role": perfil.papel_usuario,
                "institution_id": str(perfil.instituicao.id) if perfil.instituicao else None,
                "department_id": str(perfil.filial.id) if perfil.filial else None,
                "email": user.email
            }

            if perfil.papel_usuario == PapelUsuario.ADMIN_DEPARTAMENTO:
                if not perfil.filial:
                    logger.warning(f"Gestor {user.id} não vinculado a filial")
                    return Response({"error": "Gestor não vinculado a um departamento"}, status=status.HTTP_403_FORBIDDEN)

                filas = Fila.objects.filter(departamento__filial=perfil.filial)
                response_data["queues"] = [
                    {
                        'id': str(fila.id),
                        'service': fila.servico,
                        'prefix': fila.prefixo,
                        'department': fila.departamento.nome,
                        'active_tickets': fila.tickets_ativos,
                        'daily_limit': fila.limite_diario,
                        'current_ticket': fila.ticket_atual,
                        'status': 'Aberto' if fila.tickets_ativos < fila.limite_diario else 'Lotado',
                        'open_time': fila.hora_abertura.strftime('%H:%M') if fila.hora_abertura else None,
                        'end_time': fila.hora_fechamento.strftime('%H:%M') if fila.hora_fechamento else None
                    } for fila in filas
                ]

            elif perfil.papel_usuario == PapelUsuario.ADMIN_INSTITUICAO:
                if not perfil.instituicao:
                    logger.warning(f"Admin {user.id} não vinculado a instituição")
                    return Response({"error": "Admin não vinculado a uma instituição"}, status=status.HTTP_403_FORBIDDEN)

                departamentos = Departamento.objects.filter(filial__instituicao=perfil.instituicao)
                response_data["departments"] = [
                    {'id': str(d.id), 'name': d.nome, 'sector': d.setor} for d in departamentos
                ]

                gestores = PerfilUsuario.objects.filter(
                    instituicao=perfil.instituicao,
                    papel_usuario=PapelUsuario.ADMIN_DEPARTAMENTO
                )
                response_data["managers"] = [
                    {
                        'id': str(g.usuario.id),
                        'email': g.usuario.email,
                        'name': g.usuario.first_name or g.usuario.username,
                        'department_id': str(g.filial.id) if g.filial else None,
                        'department_name': g.filial.nome if g.filial else 'N/A'
                    } for g in gestores
                ]

            response = Response(response_data, status=status.HTTP_200_OK)
            response['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
            response['Access-Control-Allow-Credentials'] = 'true'
            logger.info(f"Login bem-sucedido para usuário: {email} ({perfil.papel_usuario})")
            return response

        except Exception as e:
            logger.error(f"Erro ao processar login para email={request.data.get('email', 'unknown')}: {str(e)}")
            return Response({"error": "Erro interno no servidor"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)