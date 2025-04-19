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
    
import logging
import uuid
import json
import re
from django.utils import timezone
from django.http import HttpResponse
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from geopy.distance import geodesic
from sistema.auth import FirebaseAndTokenAuthentication
from fila_online.models import Fila, Ticket, Departamento, Instituicao, Filial, HorarioFila
from sistema.models import PerfilUsuario, PreferenciaUsuario, LogAuditoria
from .services import ServicoFila
from .ml_models import preditor_tempo_espera
import redis
from django.conf import settings
from datetime import datetime
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import io

logger = logging.getLogger(__name__)

# Configuração do Redis
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

class SugerirServicoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        servico = request.query_params.get('servico')
        lat_usuario = request.query_params.get('lat')
        lon_usuario = request.query_params.get('lon')
        bairro = request.query_params.get('bairro')

        if not servico:
            logger.warning("Parâmetro 'servico' não fornecido")
            return Response({'erro': "O parâmetro 'servico' é obrigatório."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lat_usuario = float(lat_usuario) if lat_usuario else None
            lon_usuario = float(lon_usuario) if lon_usuario else None
        except (ValueError, TypeError):
            logger.warning(f"Coordenadas inválidas: lat={lat_usuario}, lon={lon_usuario}")
            return Response({'erro': 'Latitude e longitude devem ser números'}, status=status.HTTP_400_BAD_REQUEST)

        if bairro and not re.match(r'^[A-Za-zÀ-ÿ\s,]{1,100}$', bairro):
            logger.warning(f"Bairro inválido: {bairro}")
            return Response({'erro': 'Bairro inválido'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            sugestoes = ServicoFila.buscar_servicos(
                termo_busca=servico,
                usuario_id=str(request.user.id),
                lat_usuario=lat_usuario,
                lon_usuario=lon_usuario,
                bairro=bairro,
                max_resultados=10
            )
            logger.info(f"Sugestões geradas para serviço '{servico}': {sugestoes['total']} resultados")
            return Response(sugestoes, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao gerar sugestões para serviço '{servico}': {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Erro inesperado ao gerar sugestões: {e}")
            return Response({'erro': "Erro ao gerar sugestões."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AtualizarLocalizacaoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        email = data.get('email')

        if latitude is None or longitude is None:
            logger.error(f"Latitude ou longitude não fornecidos por user_id={request.user.id}")
            return Response({'erro': 'Latitude e longitude são obrigatórios'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (ValueError, TypeError):
            logger.error(f"Coordenadas inválidas: lat={latitude}, lon={longitude}")
            return Response({'erro': 'Latitude e longitude devem ser números'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            perfil, created = PerfilUsuario.objects.get_or_create(
                usuario_id=str(request.user.id),
                defaults={'email': email or f"{request.user.id}@example.com"}
            )
            perfil.ultima_latitude = latitude
            perfil.ultima_longitude = longitude
            perfil.ultima_atualizacao_local = timezone.now()
            perfil.save()
            logger.info(f"Localização atualizada para user_id={request.user.id}: lat={latitude}, lon={longitude}")

            ServicoFila.verificar_notificacoes_proximidade(str(request.user.id), latitude, longitude)
            ServicoFila.verificar_notificacoes_proativas()
            return Response({'mensagem': 'Localização atualizada com sucesso'}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao atualizar localização: {e}")
            return Response({'erro': 'Erro ao atualizar localização'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class CriarFilaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de criar fila por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        data = request.data
        required = ['servico', 'prefixo', 'departamento_id', 'hora_abertura', 'limite_diario', 'num_balcoes', 'filial_id']
        if not all(field in data for field in required):
            logger.warning("Campos obrigatórios faltando na criação de fila")
            return Response({'erro': 'Campos obrigatórios faltando'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[A-Z]$', data['prefixo']):
            logger.warning(f"Prefixo inválido: {data['prefixo']}")
            return Response({'erro': 'Prefixo deve ser uma única letra maiúscula'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(data['limite_diario'], int) or data['limite_diario'] <= 0:
            logger.warning(f"Limite diário inválido: {data['limite_diario']}")
            return Response({'erro': 'Limite diário deve ser um número positivo'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(data['num_balcoes'], int) or data['num_balcoes'] <= 0:
            logger.warning(f"Número de guichês inválido: {data['num_balcoes']}")
            return Response({'erro': 'Número de guichês deve ser um número positivo'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            departamento = Departamento.objects.get(id=data['departamento_id'])
            filial = Filial.objects.get(id=data['filial_id'])
        except ObjectDoesNotExist:
            logger.error(f"Departamento ou filial não encontrados: departamento_id={data['departamento_id']}, filial_id={data['filial_id']}")
            raise NotFound('Departamento ou filial não encontrados')

        perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))
        if request.user_tipo == 'admin_departamento' and perfil.departamento_id != data['departamento_id']:
            logger.warning(f"Usuário {request.user.id} não tem permissão para departamento {data['departamento_id']}")
            raise PermissionDenied('Sem permissão para este departamento')
        if request.user_tipo == 'admin_instituicao' and departamento.filial.instituicao_id != perfil.instituicao_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para instituição {departamento.filial.instituicao_id}")
            raise PermissionDenied('Sem permissão para esta instituição')

        if Fila.objects.filter(servico=data['servico'], departamento_id=data['departamento_id'], departamento__filial_id=data['filial_id']).exists():
            logger.warning(f"Fila já existe para serviço {data['servico']} no departamento {data['departamento_id']} e filial {data['filial_id']}")
            return Response({'erro': 'Fila já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            hora_abertura = datetime.strptime(data['hora_abertura'], '%H:%M').time()
        except ValueError:
            logger.error(f"Formato de hora_abertura inválido: {data['hora_abertura']}")
            return Response({'erro': 'Formato de hora_abertura inválido (HH:MM)'}, status=status.HTTP_400_BAD_REQUEST)

        fila = Fila(
            id=uuid.uuid4(),
            departamento_id=data['departamento_id'],
            servico=data['servico'],
            prefixo=data['prefixo'],
            hora_abertura=hora_abertura,
            limite_diario=data['limite_diario'],
            num_balcoes=data['num_balcoes'],
            tempo_espera_medio=0.0
        )
        fila.save()
        logger.info(f"Fila criada: {fila.servico} (ID: {fila.id})")
        return Response({'mensagem': f'Fila {data["servico"]} criada', 'fila_id': str(fila.id)}, status=status.HTTP_201_CREATED)

class AtualizarFilaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def put(self, request, id):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de atualizar fila por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        try:
            fila = Fila.objects.get(id=id)
        except ObjectDoesNotExist:
            logger.error(f"Fila não encontrada: id={id}")
            raise NotFound('Fila não encontrada')

        data = request.data
        perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))

        if request.user_tipo == 'admin_departamento' and fila.departamento_id != perfil.departamento_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para fila {id}")
            raise PermissionDenied('Sem permissão para esta fila')
        if request.user_tipo == 'admin_instituicao' and fila.departamento.filial.instituicao_id != perfil.instituicao_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para instituição {fila.departamento.filial.instituicao_id}")
            raise PermissionDenied('Sem permissão para esta instituição')

        if 'prefixo' in data and not re.match(r'^[A-Z]$', data['prefixo']):
            logger.warning(f"Prefixo inválido: {data['prefixo']}")
            return Response({'erro': 'Prefixo deve ser uma única letra maiúscula'}, status=status.HTTP_400_BAD_REQUEST)
        if 'limite_diario' in data and (not isinstance(data['limite_diario'], int) or data['limite_diario'] <= 0):
            logger.warning(f"Limite diário inválido: {data['limite_diario']}")
            return Response({'erro': 'Limite diário deve ser um número positivo'}, status=status.HTTP_400_BAD_REQUEST)
        if 'num_balcoes' in data and (not isinstance(data['num_balcoes'], int) or data['num_balcoes'] <= 0):
            logger.warning(f"Número de guichês inválido: {data['num_balcoes']}")
            return Response({'erro': 'Número de guichês deve ser um número positivo'}, status=status.HTTP_400_BAD_REQUEST)

        fila.servico = data.get('servico', fila.servico)
        fila.prefixo = data.get('prefixo', fila.prefixo)
        if 'departamento_id' in data:
            try:
                departamento = Departamento.objects.get(id=data['departamento_id'])
                fila.departamento = departamento
            except ObjectDoesNotExist:
                logger.error(f"Departamento não encontrado: departamento_id={data['departamento_id']}")
                raise NotFound('Departamento não encontrado')
        if 'filial_id' in data:
            try:
                filial = Filial.objects.get(id=data['filial_id'])
                fila.departamento.filial = filial
            except ObjectDoesNotExist:
                logger.error(f"Filial não encontrada: filial_id={data['filial_id']}")
                raise NotFound('Filial não encontrada')
        if 'hora_abertura' in data:
            try:
                fila.hora_abertura = datetime.strptime(data['hora_abertura'], '%H:%M').time()
            except ValueError:
                logger.error(f"Formato de hora_abertura inválido: {data['hora_abertura']}")
                return Response({'erro': 'Formato de hora_abertura inválido (HH:MM)'}, status=status.HTTP_400_BAD_REQUEST)
        fila.limite_diario = data.get('limite_diario', fila.limite_diario)
        fila.num_balcoes = data.get('num_balcoes', fila.num_balcoes)
        fila.save()
        logger.info(f"Fila atualizada: {fila.servico} (ID: {id})")
        return Response({'mensagem': 'Fila atualizada'}, status=status.HTTP_200_OK)

class ExcluirFilaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, id):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de excluir fila por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        try:
            fila = Fila.objects.get(id=id)
        except ObjectDoesNotExist:
            logger.error(f"Fila não encontrada: id={id}")
            raise NotFound('Fila não encontrada')

        perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))
        if request.user_tipo == 'admin_departamento' and fila.departamento_id != perfil.departamento_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para excluir fila {id}")
            raise PermissionDenied('Sem permissão para esta fila')
        if request.user_tipo == 'admin_instituicao' and fila.departamento.filial.instituicao_id != perfil.instituicao_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para excluir instituição {fila.departamento.filial.instituicao_id}")
            raise PermissionDenied('Sem permissão para esta instituição')

        if Ticket.objects.filter(fila_id=id, status='Pendente').exists():
            logger.warning(f"Tentativa de excluir fila {id} com senhas pendentes")
            return Response({'erro': 'Não é possível excluir: fila possui senhas pendentes'}, status=status.HTTP_400_BAD_REQUEST)

        fila.delete()
        redis_client.delete(f"cache:servicos:*")  # Invalida cache de busca
        logger.info(f"Fila excluída: {id}")
        return Response({'mensagem': 'Fila excluída'}, status=status.HTTP_200_OK)

class EmitirSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, servico):
        data = request.data
        usuario_id = data.get('usuario_id', str(request.user.id))
        token_fcm = data.get('token_fcm')
        prioridade = data.get('prioridade', 0)
        e_fisico = data.get('e_fisico', False)
        filial_id = data.get('filial_id')

        if e_fisico and not filial_id:
            logger.warning("filial_id é obrigatório para senhas físicas")
            return Response({'erro': 'filial_id é obrigatório para senhas físicas'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            senha, pdf_buffer = ServicoFila.adicionar_a_fila(
                servico=servico,
                usuario_id=usuario_id,
                prioridade=prioridade,
                e_fisico=e_fisico,
                token_fcm=token_fcm,
                filial_id=filial_id
            )

            tempo_espera = ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade)
            posicao = max(0, senha.numero_ticket - senha.fila.ticket_atual)

            resposta = {
                'mensagem': 'Senha emitida',
                'senha': {
                    'id': str(senha.id),
                    'numero': f"{senha.fila.prefixo}{senha.numero_ticket}",
                    'codigo_qr': senha.codigo_qr,
                    'tempo_espera': f"{int(tempo_espera)} minutos" if tempo_espera != "N/A" else "N/A",
                    'recibo': senha.dados_recibo,
                    'prioridade': senha.prioridade,
                    'e_fisico': senha.e_fisico,
                    'expira_em': senha.expira_em.isoformat() if senha.expira_em else None,
                    'filial_id': str(senha.fila.departamento.filial_id)
                }
            }

            # Enviar atualização via WebSocket
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                        "posicao": posicao,
                        "tempo_espera": f"{int(tempo_espera)} minutos" if tempo_espera != "N/A" else "N/A"
                    }
                }
            )

            if e_fisico and pdf_buffer:
                return HttpResponse(
                    pdf_buffer.getvalue(),
                    headers={
                        'Content-Type': 'application/pdf',
                        'Content-Disposition': f'attachment; filename=senha_{senha.fila.prefixo}{senha.numero_ticket}.pdf'
                    }
                )

            logger.info(f"Senha emitida: {senha.fila.prefixo}{senha.numero_ticket} para usuario_id={usuario_id}")
            ServicoFila.verificar_notificacoes_proativas()
            return Response(resposta, status=status.HTTP_201_CREATED)
        except ValueError as e:
            logger.error(f"Erro ao emitir senha para serviço {servico}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)

class BaixarSenhaPDFView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, senha_id):
        try:
            senha = Ticket.objects.get(id=senha_id)
        except ObjectDoesNotExist:
            logger.error(f"Senha não encontrada: id={senha_id}")
            raise NotFound('Senha não encontrada')

        if senha.usuario_id != str(request.user.id) and senha.usuario_id != 'PRESENCIAL':
            logger.warning(f"Tentativa não autorizada de baixar PDF da senha {senha_id} por usuario_id={request.user.id}")
            raise PermissionDenied('Não autorizado')

        try:
            pdf_buffer = ServicoFila.gerar_pdf_senha(senha)
            logger.info(f"PDF gerado para senha {senha_id}")
            return HttpResponse(
                pdf_buffer.getvalue(),
                headers={
                    'Content-Type': 'application/pdf',
                    'Content-Disposition': f'attachment; filename=senha_{senha.fila.prefixo}{senha.numero_ticket}.pdf'
                }
            )
        except Exception as e:
            logger.error(f"Erro ao gerar PDF para senha {senha_id}: {e}")
            return Response({'erro': 'Erro ao gerar PDF'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class StatusSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, senha_id):
        try:
            senha = Ticket.objects.get(id=senha_id)
        except ObjectDoesNotExist:
            logger.error(f"Senha não encontrada: id={senha_id}")
            raise NotFound('Senha não encontrada')

        if senha.usuario_id != str(request.user.id) and senha.usuario_id != 'PRESENCIAL':
            logger.warning(f"Tentativa não autorizada de visualizar status da senha {senha_id} por usuario_id={request.user.id}")
            raise PermissionDenied('Não autorizado')

        fila = senha.fila
        tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, senha.numero_ticket, senha.prioridade)
        posicao = max(0, senha.numero_ticket - fila.ticket_atual)

        return Response({
            'servico': fila.servico,
            'instituicao': fila.departamento.filial.instituicao.nome,
            'filial': fila.departamento.filial.nome,
            'numero_senha': f"{fila.prefixo}{senha.numero_ticket}",
            'codigo_qr': senha.codigo_qr,
            'status': senha.status,
            'balcao': f"{senha.balcao:02d}" if senha.balcao else None,
            'posicao': posicao,
            'tempo_espera': f"{int(tempo_espera)} minutos" if tempo_espera != "N/A" else "N/A",
            'prioridade': senha.prioridade,
            'e_fisico': senha.e_fisico,
            'expira_em': senha.expira_em.isoformat() if senha.expira_em else None
        }, status=status.HTTP_200_OK)

class ChamarProximaSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, servico):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de chamar senha por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        try:
            senha = ServicoFila.chamar_proximo(servico)
            perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))

            if request.user_tipo == 'admin_departamento' and senha.fila.departamento_id != perfil.departamento_id:
                logger.warning(f"Usuário {request.user.id} não tem permissão para fila {senha.fila_id}")
                raise PermissionDenied('Sem permissão para esta fila')
            if request.user_tipo == 'admin_instituicao' and senha.fila.departamento.filial.instituicao_id != perfil.instituicao_id:
                logger.warning(f"Usuário {request.user.id} não tem permissão para instituição {senha.fila.departamento.filial.instituicao_id}")
                raise PermissionDenied('Sem permissão para esta instituição')

            # Enviar atualização via WebSocket
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                        "posicao": 0,
                        "tempo_espera": "0 minutos"
                    }
                }
            )
            async_to_sync(camada_canal.group_send)(
                f"painel_{senha.fila.departamento.filial.instituicao_id}",
                {
                    "type": "atualizacao_painel",
                    "mensagem": {
                        "instituicao_id": str(senha.fila.departamento.filial.instituicao_id),
                        "fila_id": str(senha.fila_id),
                        "tipo_evento": "nova_chamada",
                        "dados": {
                            "numero_senha": f"{senha.fila.prefixo}{senha.numero_ticket}",
                            "balcao": senha.balcao,
                            "timestamp": senha.atendido_em.isoformat()
                        }
                    }
                }
            )

            logger.info(f"Senha chamada: {senha.fila.prefixo}{senha.numero_ticket} (senha_id={senha.id})")
            return Response({
                'mensagem': f'Senha {senha.fila.prefixo}{senha.numero_ticket} chamada',
                'senha_id': str(senha.id),
                'restantes': senha.fila.tickets_ativos
            }, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao chamar próxima senha para serviço {servico}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)

class ChamarSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, senha_id):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de chamar senha {senha_id} por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        try:
            senha = Ticket.objects.get(id=senha_id)
        except ObjectDoesNotExist:
            logger.error(f"Senha não encontrada: id={senha_id}")
            raise NotFound('Senha não encontrada')

        if senha.status != 'Pendente':
            logger.warning(f"Tentativa de chamar senha {senha_id} com status {senha.status}")
            return Response({'erro': f'Senha já está {senha.status}'}, status=status.HTTP_400_BAD_REQUEST)

        perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))
        if request.user_tipo == 'admin_departamento' and senha.fila.departamento_id != perfil.departamento_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para chamar senha {senha_id}")
            raise PermissionDenied('Sem permissão para esta fila')
        if request.user_tipo == 'admin_instituicao' and senha.fila.departamento.filial.instituicao_id != perfil.instituicao_id:
            logger.warning(f"Usuário {request.user.id} não tem permissão para instituição {senha.fila.departamento.filial.instituicao_id}")
            raise PermissionDenied('Sem permissão para esta instituição')

        try:
            data = request.data
            balcao = data.get('balcao', senha.fila.ultimo_balcao or 1)
            senha.status = 'Chamado'
            senha.atendido_em = timezone.now()
            senha.balcao = balcao
            senha.fila.ticket_atual = senha.numero_ticket
            senha.fila.tickets_ativos -= 1
            senha.fila.ultimo_balcao = balcao
            senha.fila.save()
            senha.save()

            # Enviar atualização via WebSocket
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                        "posicao": 0,
                        "tempo_espera": "0 minutos"
                    }
                }
            )
            async_to_sync(camada_canal.group_send)(
                f"painel_{senha.fila.departamento.filial.instituicao_id}",
                {
                    "type": "atualizacao_painel",
                    "mensagem": {
                        "instituicao_id": str(senha.fila.departamento.filial.instituicao_id),
                        "fila_id": str(senha.fila_id),
                        "tipo_evento": "nova_chamada",
                        "dados": {
                            "numero_senha": f"{senha.fila.prefixo}{senha.numero_ticket}",
                            "balcao": senha.balcao,
                            "timestamp": senha.atendido_em.isoformat()
                        }
                    }
                }
            )

            logger.info(f"Senha {senha_id} chamada com sucesso: {senha.fila.prefixo}{senha.numero_ticket}")
            ServicoFila.verificar_notificacoes_proativas()
            return Response({
                'mensagem': 'Senha chamada com sucesso',
                'senha': {
                    'id': str(senha.id),
                    'numero': f"{senha.fila.prefixo}{senha.numero_ticket}",
                    'status': senha.status,
                    'balcao': senha.balcao
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao chamar senha {senha_id}: {e}")
            return Response({'erro': f'Erro ao chamar senha: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class OferecerTrocaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, senha_id):
        try:
            senha = ServicoFila.oferecer_troca(senha_id, str(request.user.id))
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                        "posicao": max(0, senha.numero_ticket - senha.fila.ticket_atual),
                        "tempo_espera": f"{int(ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade))} minutos"
                    }
                }
            )
            logger.info(f"Senha oferecida para troca: {senha_id}")
            return Response({'mensagem': 'Senha oferecida para troca', 'senha_id': str(senha.id)}, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao oferecer troca para senha {senha_id}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)

class TrocarSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, senha_para_id):
        senha_de_id = request.data.get('senha_de_id')
        try:
            resultado = ServicoFila.trocar_senhas(senha_de_id, senha_para_id, str(request.user.id))
            camada_canal = get_channel_layer()
            for senha in [resultado['senha_de'], resultado['senha_para']]:
                async_to_sync(camada_canal.group_send)(
                    f"senha_{senha.id}",
                    {
                        "type": "atualizacao_senha",
                        "mensagem": {
                            "senha_id": str(senha.id),
                            "status": senha.status,
                            "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                            "posicao": max(0, senha.numero_ticket - senha.fila.ticket_atual),
                            "tempo_espera": f"{int(ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade))} minutos"
                        }
                    }
                )
            logger.info(f"Troca realizada entre senhas {senha_de_id} e {senha_para_id}")
            return Response({
                'mensagem': 'Troca realizada',
                'senhas': {
                    'de': {'id': str(resultado['senha_de'].id), 'numero': f"{resultado['senha_de'].fila.prefixo}{resultado['senha_de'].numero_ticket}"},
                    'para': {'id': str(resultado['senha_para'].id), 'numero': f"{resultado['senha_para'].fila.prefixo}{resultado['senha_para'].numero_ticket}"}
                }
            }, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao realizar troca entre senhas {senha_de_id} e {senha_para_id}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)

class ValidarSenhaView(APIView):
    def post(self, request):
        data = request.data
        codigo_qr = data.get('codigo_qr')
        numero_senha = data.get('numero_senha')
        fila_id = data.get('fila_id')
        lat_usuario = data.get('lat_usuario')
        lon_usuario = data.get('lon_usuario')

        if not codigo_qr and not (numero_senha and fila_id):
            logger.warning("Requisição de validação sem codigo_qr ou numero_senha/fila_id")
            return Response({'erro': 'Forneça codigo_qr ou numero_senha e fila_id'}, status=status.HTTP_400_BAD_REQUEST)

        if numero_senha is not None:
            try:
                numero_senha = int(numero_senha)
            except (ValueError, TypeError):
                logger.warning(f"numero_senha inválido: {numero_senha}")
                return Response({'erro': 'numero_senha deve ser um número inteiro'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lat_usuario = float(lat_usuario) if lat_usuario else None
            lon_usuario = float(lon_usuario) if lon_usuario else None
        except (ValueError, TypeError):
            logger.warning(f"Coordenadas inválidas: lat={lat_usuario}, lon={lon_usuario}")
            return Response({'erro': 'Latitude e longitude devem ser números'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            senha = ServicoFila.validar_presenca(codigo_qr=codigo_qr, lat_usuario=lat_usuario, lon_usuario=lon_usuario)
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": f"{senha.balcao:02d}" if senha.balcao else None,
                        "posicao": 0,
                        "tempo_espera": "0 minutos"
                    }
                }
            )
            async_to_sync(camada_canal.group_send)(
                f"painel_{senha.fila.departamento.filial.instituicao_id}",
                {
                    "type": "atualizacao_painel",
                    "mensagem": {
                        "instituicao_id": str(senha.fila.departamento.filial.instituicao_id),
                        "fila_id": str(senha.fila_id),
                        "tipo_evento": "chamada_concluida",
                        "dados": {
                            "numero_senha": f"{senha.fila.prefixo}{senha.numero_ticket}",
                            "balcao": senha.balcao,
                            "timestamp": senha.atendido_em.isoformat()
                        }
                    }
                }
            )
            logger.info(f"Presença validada para senha {senha.id}")
            return Response({'mensagem': 'Presença validada com sucesso', 'senha_id': str(senha.id)}, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao validar senha (codigo_qr={codigo_qr}, numero_senha={numero_senha}): {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Erro inesperado ao validar senha: {e}")
            return Response({'erro': f'Erro ao validar senha: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarFilasView(APIView):
    def get(self, request):
        instituicoes = Instituicao.objects.all()
        agora = timezone.now()
        dia_semana = agora.strftime('%A').capitalize()
        hora_atual = agora.time()
        resultado = []

        for inst in instituicoes:
            filiais = Filial.objects.filter(instituicao_id=inst.id)
            departamentos = Departamento.objects.filter(instituicao_id=inst.id)
            filas = Fila.objects.filter(departamento_id__in=[d.id for d in departamentos])
            dados_filas = []

            for fila in filas:
                horario = HorarioFila.objects.filter(fila_id=fila.id, dia_semana=dia_semana).first()
                esta_aberta = False
                if horario and not horario.esta_fechado:
                    esta_aberta = (
                        horario.hora_abertura and horario.hora_fechamento and
                        hora_atual >= horario.hora_abertura and
                        hora_atual <= horario.hora_fechamento and
                        fila.tickets_ativos < fila.limite_diario
                    )

                tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, fila.ticket_atual + 1, 0)

                dados_filas.append({
                    'id': str(fila.id),
                    'servico': fila.servico,
                    'prefixo': fila.prefixo,
                    'setor': fila.departamento.setor if fila.departamento else None,
                    'departamento': fila.departamento.nome if fila.departamento else None,
                    'filial': fila.departamento.filial.nome if fila.departamento.filial else None,
                    'instituicao': fila.departamento.filial.instituicao.nome if fila.departamento.filial else None,
                    'hora_abertura': horario.hora_abertura.strftime('%H:%M') if horario and horario.hora_abertura else None,
                    'hora_fechamento': horario.hora_fechamento.strftime('%H:%M') if horario and horario.hora_fechamento else None,
                    'limite_diario': fila.limite_diario,
                    'tickets_ativos': fila.tickets_ativos,
                    'tempo_espera_medio': f"{int(tempo_espera)} minutos" if tempo_espera != "N/A" else "N/A",
                    'num_balcoes': fila.num_balcoes,
                    'status': 'Aberta' if esta_aberta else 'Fechada'
                })

            resultado.append({
                'instituicao': {
                    'id': str(inst.id),
                    'nome': inst.nome,
                    'localizacao': inst.localizacao,
                    'latitude': inst.latitude,
                    'longitude': inst.longitude
                },
                'filas': dados_filas
            })

        logger.info(f"Lista de filas retornada: {len(resultado)} instituições encontradas")
        return Response(resultado, status=status.HTTP_200_OK)

class ListarSenhasUsuarioView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        senhas = Ticket.objects.filter(usuario_id=str(request.user.id))
        resultado = [{
            'id': str(senha.id),
            'servico': senha.fila.servico,
            'instituicao': senha.fila.departamento.filial.instituicao.nome,
            'filial': senha.fila.departamento.filial.nome,
            'numero': f"{senha.fila.prefixo}{senha.numero_ticket}",
            'status': senha.status,
            'balcao': f"{senha.balcao:02d}" if senha.balcao else None,
            'posicao': max(0, senha.numero_ticket - senha.fila.ticket_atual) if senha.status == 'Pendente' else 0,
            'tempo_espera': f"{int(ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade))} minutos" if senha.status == 'Pendente' else "N/A",
            'codigo_qr': senha.codigo_qr,
            'troca_disponivel': senha.troca_disponivel
        } for senha in senhas]
        return Response(resultado, status=status.HTTP_200_OK)

class ListarSenhasTrocaDisponivelView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        usuario_id = str(request.user.id)
        senhas_usuario = Ticket.objects.filter(usuario_id=usuario_id, status='Pendente')
        filas_ids = {senha.fila_id for senha in senhas_usuario}

        if not filas_ids:
            return Response([], status=status.HTTP_200_OK)

        senhas = Ticket.objects.filter(
            fila_id__in=filas_ids,
            troca_disponivel=True,
            status='Pendente',
            usuario_id__ne=usuario_id
        )

        return Response([{
            'id': str(senha.id),
            'servico': senha.fila.servico,
            'instituicao': senha.fila.departamento.filial.instituicao.nome,
            'filial': senha.fila.departamento.filial.nome,
            'numero': f"{senha.fila.prefixo}{senha.numero_ticket}",
            'posicao': max(0, senha.numero_ticket - senha.fila.ticket_atual),
            'usuario_id': senha.usuario_id
        } for senha in senhas], status=status.HTTP_200_OK)

class CancelarSenhaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, senha_id):
        try:
            senha = ServicoFila.cancelar_senha(senha_id, str(request.user.id))
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha.id}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": str(senha.id),
                        "status": senha.status,
                        "balcao": None,
                        "posicao": 0,
                        "tempo_espera": "N/A"
                    }
                }
            )
            logger.info(f"Senha cancelada: {senha.fila.prefixo}{senha.numero_ticket} (senha_id={senha.id})")
            return Response({'mensagem': f'Senha {senha.fila.prefixo}{senha.numero_ticket} cancelada', 'senha_id': str(senha.id)}, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao cancelar senha {senha_id}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)

class ListarTodasSenhasView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user_tipo not in ['admin_departamento', 'admin_instituicao', 'admin_sistema']:
            logger.warning(f"Tentativa não autorizada de listar senhas por user_id={request.user.id}")
            raise PermissionDenied('Acesso restrito a administradores')

        perfil = PerfilUsuario.objects.get(usuario_id=str(request.user.id))
        if request.user_tipo == 'admin_sistema':
            senhas = Ticket.objects.all()
        elif request.user_tipo == 'admin_instituicao':
            senhas = Ticket.objects.filter(fila__departamento__filial__instituicao_id=perfil.instituicao_id)
        else:
            senhas = Ticket.objects.filter(fila__departamento_id=perfil.departamento_id)

        return Response([{
            'id': str(senha.id),
            'servico': senha.fila.servico,
            'instituicao': senha.fila.departamento.filial.instituicao.nome,
            'filial': senha.fila.departamento.filial.nome,
            'numero': f"{senha.fila.prefixo}{senha.numero_ticket}",
            'status': senha.status,
            'balcao': f"{senha.balcao:02d}" if senha.balcao else None,
            'posicao': max(0, senha.numero_ticket - senha.fila.ticket_atual) if senha.status == 'Pendente' else 0,
            'tempo_espera': f"{int(ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade))} minutos" if senha.status == 'Pendente' else "N/A",
            'codigo_qr': senha.codigo_qr,
            'troca_disponivel': senha.troca_disponivel,
            'usuario_id': senha.usuario_id
        } for senha in senhas], status=status.HTTP_200_OK)

class AtualizarTokenFCMView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        token_fcm = data.get('token_fcm')
        email = data.get('email')

        if not token_fcm or not email:
            logger.error(f"Token FCM ou email não fornecidos por user_id={request.user.id}")
            return Response({'erro': 'Token FCM e email são obrigatórios'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            perfil, created = PerfilUsuario.objects.get_or_create(
                usuario_id=str(request.user.id),
                defaults={'email': email}
            )
            if not created and perfil.email != email:
                logger.warning(f"Email fornecido ({email}) não corresponde ao user_id={request.user.id}")
                return Response({'erro': 'Email não corresponde ao usuário autenticado'}, status=status.HTTP_403_FORBIDDEN)
            perfil.token_fcm = token_fcm
            perfil.save()
            logger.info(f"Token FCM atualizado para user_id={request.user.id}, email={email}")
            ServicoFila.verificar_notificacoes_proximidade(str(request.user.id), perfil.ultima_latitude, perfil.ultima_longitude)
            return Response({'mensagem': 'Token FCM atualizado com sucesso'}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao atualizar token FCM: {e}")
            return Response({'erro': 'Erro ao atualizar token FCM'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class SenhaAtualView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, nome_instituicao, servico):
        try:
            instituicao = Instituicao.objects.get(nome=nome_instituicao)
        except ObjectDoesNotExist:
            logger.error(f"Instituição não encontrada: {nome_instituicao}")
            raise NotFound('Instituição não encontrada')

        try:
            departamento = Departamento.objects.filter(instituicao_id=instituicao.id).first()
            if not departamento:
                logger.error(f"Departamento não encontrado para instituicao={nome_instituicao}")
                raise NotFound('Departamento não encontrado')
            fila = Fila.objects.get(departamento_id=departamento.id, servico=servico)
        except ObjectDoesNotExist:
            logger.error(f"Fila não encontrada para instituicao={nome_instituicao}, servico={servico}")
            raise NotFound('Fila não encontrada')

        if fila.ticket_atual == 0:
            return Response({'senha_atual': 'N/A'}, status=status.HTTP_200_OK)

        return Response({'senha_atual': f"{fila.prefixo}{fila.ticket_atual:03d}"}, status=status.HTTP_200_OK)

class CalcularDistanciaView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        lat_usuario = data.get('latitude')
        lon_usuario = data.get('longitude')
        instituicao_id = data.get('instituicao_id')

        if not all([lat_usuario, lon_usuario, instituicao_id]):
            logger.warning("Requisição de distância sem latitude, longitude ou instituicao_id")
            return Response({'erro': 'Latitude, longitude e instituicao_id são obrigatórios'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lat_usuario = float(lat_usuario)
            lon_usuario = float(lon_usuario)
        except (ValueError, TypeError):
            logger.warning(f"Coordenadas inválidas: lat={lat_usuario}, lon={lon_usuario}")
            return Response({'erro': 'Latitude e longitude devem ser números'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            instituicao = Instituicao.objects.get(id=instituicao_id)
        except ObjectDoesNotExist:
            logger.error(f"Instituição não encontrada: instituicao_id={instituicao_id}")
            raise NotFound('Instituição não encontrada')

        distancia = ServicoFila.calcular_distancia(lat_usuario, lon_usuario, instituicao)
        if distancia is None:
            logger.error(f"Erro ao calcular distância para instituicao_id={instituicao_id}")
            return Response({'erro': 'Erro ao calcular distância'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        logger.info(f"Distância calculada: {distancia:.2f} km entre usuário ({lat_usuario}, {lon_usuario}) e {instituicao.nome}")
        return Response({'distancia': distancia}, status=status.HTTP_200_OK)

class BuscarServicosInstituicaoView(APIView):
    def get(self, request, instituicao_id):
        try:
            instituicao = Instituicao.objects.get(id=instituicao_id)
        except ObjectDoesNotExist:
            logger.error(f"Instituição não encontrada: id={instituicao_id}")
            raise NotFound('Instituição não encontrada')

        usuario_id = str(request.user.id) if request.user.is_authenticated else None
        filtros = {}
        nome_servico = request.query_params.get('nome_servico')
        if nome_servico:
            if not re.match(r'^[A-Za-zÀ-ÿ\s]{1,100}$', nome_servico):
                logger.warning(f"Nome do serviço inválido: {nome_servico}")
                return Response({'erro': 'Nome do serviço inválido'}, status=status.HTTP_400_BAD_REQUEST)
            filtros['termo_busca'] = nome_servico

        categoria_id = request.query_params.get('categoria_id')
        if categoria_id:
            try:
                from fila_online.models import Categoria
                Categoria.objects.get(id=categoria_id)
            except ObjectDoesNotExist:
                logger.warning(f"Categoria inválida: {categoria_id}")
                return Response({'erro': 'Categoria inválida'}, status=status.HTTP_400_BAD_REQUEST)
            filtros['categoria_id'] = categoria_id

        etiqueta = request.query_params.get('etiqueta')
        if etiqueta:
            try:
                from fila_online.models import EtiquetaServico
                EtiquetaServico.objects.filter(etiqueta=etiqueta).first()
            except ObjectDoesNotExist:
                logger.warning(f"Etiqueta inválida: {etiqueta}")
                return Response({'erro': 'Etiqueta inválida'}, status=status.HTTP_400_BAD_REQUEST)
            filtros['etiqueta'] = etiqueta

        lat_usuario = request.query_params.get('latitude')
        lon_usuario = request.query_params.get('longitude')
        if lat_usuario and lon_usuario:
            try:
                filtros['lat_usuario'] = float(lat_usuario)
                filtros['lon_usuario'] = float(lon_usuario)
            except (ValueError, TypeError):
                logger.warning(f"Coordenadas inválidas: lat={lat_usuario}, lon={lon_usuario}")
                return Response({'erro': 'Latitude e longitude devem ser números'}, status=status.HTTP_400_BAD_REQUEST)

        bairro = request.query_params.get('bairro')
        if bairro:
            if not re.match(r'^[A-Za-zÀ-ÿ\s,]{1,100}$', bairro):
                logger.warning(f"Bairro inválido: {bairro}")
                return Response({'erro': 'Bairro inválido'}, status=status.HTTP_400_BAD_REQUEST)
            filtros['bairro'] = bairro

        tempo_espera_max = request.query_params.get('tempo_espera_max')
        if tempo_espera_max is not None:
            try:
                tempo_espera_max = int(tempo_espera_max)
                if tempo_espera_max < 0 or tempo_espera_max > 1440:
                    logger.warning(f"Tempo de espera inválido: {tempo_espera_max}")
                    return Response({'erro': 'Tempo de espera inválido'}, status=status.HTTP_400_BAD_REQUEST)
                filtros['tempo_espera_max'] = tempo_espera_max
            except (ValueError, TypeError):
                logger.warning(f"Tempo de espera inválido: {tempo_espera_max}")
                return Response({'erro': 'Tempo de espera deve ser um número inteiro'}, status=status.HTTP_400_BAD_REQUEST)

        esta_aberta = request.query_params.get('esta_aberta', 'true').lower() == 'true'
        filtros['esta_aberta'] = esta_aberta

        pagina = request.query_params.get('pagina', '1')
        por_pagina = request.query_params.get('por_pagina', '20')
        try:
            pagina = int(pagina)
            por_pagina = int(por_pagina)
        except (ValueError, TypeError):
            logger.warning(f"Página ou por_pagina inválidos: pagina={pagina}, por_pagina={por_pagina}")
            return Response({'erro': 'Página e itens por página devem ser números inteiros'}, status=status.HTTP_400_BAD_REQUEST)

        if por_pagina > 100:
            logger.warning(f"Por_pagina excede o máximo: {por_pagina}")
            return Response({'erro': 'Máximo de itens por página é 100'}, status=status.HTTP_400_BAD_REQUEST)

        filtros['pagina'] = pagina
        filtros['por_pagina'] = por_pagina

        try:
            resultado = ServicoFila.buscar_servicos(
                termo_busca=nome_servico,
                usuario_id=usuario_id,
                instituicao_id=instituicao_id,
                **filtros
            )
            logger.info(f"Serviços buscados para instituicao_id={instituicao_id}: {resultado['total']} resultados")
            return Response(resultado, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao buscar serviços para instituicao_id={instituicao_id}: {e}")
            return Response({'erro': f'Erro ao buscar serviços: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class GerarSenhaFisicaView(APIView):
    def post(self, request, instituicao_id):
        try:
            instituicao = Instituicao.objects.get(id=instituicao_id)
        except ObjectDoesNotExist:
            logger.error(f"Instituição não encontrada: id={instituicao_id}")
            raise NotFound('Instituição não encontrada')

        data = request.data
        fila_id = data.get('fila_id')
        filial_id = data.get('filial_id')

        if not fila_id or not filial_id:
            logger.warning(f"fila_id ou filial_id inválidos: fila_id={fila_id}, filial_id={filial_id}")
            return Response({'erro': 'fila_id e filial_id são obrigatórios'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            fila = Fila.objects.get(id=fila_id)
            if fila.departamento.filial.instituicao_id != instituicao_id or fila.departamento.filial_id != filial_id:
                logger.warning(f"Fila {fila_id} não pertence à instituicao_id={instituicao_id} ou filial_id={filial_id}")
                raise NotFound('Fila não encontrada ou não pertence à instituição/filial')
        except ObjectDoesNotExist:
            logger.warning(f"Fila não encontrada: fila_id={fila_id}")
            raise NotFound('Fila não encontrada')

        ip_cliente = request.META.get('REMOTE_ADDR')
        try:
            resultado = ServicoFila.gerar_senha_fisica_para_totem(fila_id, ip_cliente)
            senha = resultado['senha']
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"senha_{senha['id']}",
                {
                    "type": "atualizacao_senha",
                    "mensagem": {
                        "senha_id": senha['id'],
                        "status": senha['status'],
                        "balcao": None,
                        "posicao": max(0, senha['numero_senha'] - fila.ticket_atual),
                        "tempo_espera": f"{int(ServicoFila.calcular_tempo_espera(fila_id, senha['numero_senha'], 0))} minutos"
                    }
                }
            )
            logger.info(f"Senha física gerada para fila_id={fila_id}, instituicao_id={instituicao_id}")
            return HttpResponse(
                bytes.fromhex(resultado['pdf']),
                headers={
                    'Content-Type': 'application/pdf',
                    'Content-Disposition': f'attachment; filename=senha_{fila.prefixo}{senha["numero_senha"]}.pdf'
                }
            )
        except ValueError as e:
            logger.error(f"Erro ao gerar senha física para fila_id={fila_id}: {e}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Erro inesperado ao gerar senha física para fila_id={fila_id}: {e}")
            return Response({'erro': f'Erro ao gerar senha: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class PainelView(APIView):
    def get(self, request, instituicao_id):
        try:
            instituicao = Instituicao.objects.get(id=instituicao_id)
        except ObjectDoesNotExist:
            logger.error(f"Instituição não encontrada: id={instituicao_id}")
            raise NotFound('Instituição não encontrada')

        atualizar = request.query_params.get('atualizar', 'false').lower() == 'true'
        chave_cache = f'painel:{instituicao_id}'
        if not atualizar:
            try:
                dados_cache = redis_client.get(chave_cache)
                if dados_cache:
                    return Response(json.loads(dados_cache), status=status.HTTP_200_OK)
            except Exception as e:
                logger.warning(f"Erro ao acessar Redis para painel {instituicao_id}: {e}")

        dados = ServicoFila.obter_dados_painel(instituicao_id)
        try:
            redis_client.setex(chave_cache, 300, json.dumps(dados))
        except Exception as e:
            logger.warning(f"Erro ao salvar cache no Redis para painel {instituicao_id}: {e}")
        return Response(dados, status=status.HTTP_200_OK)

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from django.contrib.auth.hashers import make_password
from fila_online.models import Instituicao, Filial, Departamento, Fila, Ticket, HorarioFila
from sistema.models import User, PerfilUsuario
from .services import ServicoFila
from sistema.auth import FirebaseAndTokenAuthentication
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import uuid
import re
import logging
import json
import redis
from datetime import datetime, timedelta
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
redis_client = redis.Redis.from_url('redis://localhost:6379')

class CriarInstituicaoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request):
        user = User.objects.get(id=request.user.id)
        if user.user_tipo != 'admin_sistema':
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super administradores'}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        if not data or 'nome' not in data:
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campo obrigatório: nome'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[A-Za-zÀ-ÿ\s0-9.,-]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome da instituição inválido'}, status=status.HTTP_400_BAD_REQUEST)

        if Instituicao.objects.filter(nome=data['nome']).exists():
            logger.warning(f"Instituição com nome {data['nome']} já existe")
            return Response({'erro': 'Instituição com este nome já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            instituicao = Instituicao(
                id=uuid.uuid4(),
                nome=data['nome'],
                descricao=data.get('descricao')
            )
            instituicao.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'admin_global',
                {
                    'type': 'instituicao_criada',
                    'instituicao_id': str(instituicao.id),
                    'nome': instituicao.nome,
                    'descricao': instituicao.descricao
                }
            )
            redis_client.delete('cache:search:*')
            logger.info(f"Instituição {instituicao.nome} criada por user_id={user.id}")
            return Response({
                'mensagem': 'Instituição criada com sucesso',
                'instituicao': {
                    'id': str(instituicao.id),
                    'nome': instituicao.nome,
                    'descricao': instituicao.descricao
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao criar instituição: {str(e)}")
            return Response({'erro': 'Erro interno ao criar instituição'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AtualizarInstituicaoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def put(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        if user.user_tipo != 'admin_sistema':
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super administradores'}, status=status.HTTP_403_FORBIDDEN)

        instituicao = get_object_or_404(Instituicao, id=instituicao_id)
        data = request.data
        if not data:
            logger.warning("Nenhum dado fornecido")
            return Response({'erro': 'Nenhum dado fornecido para atualização'}, status=status.HTTP_400_BAD_REQUEST)

        if 'nome' in data and not re.match(r'^[A-Za-zÀ-ÿ\s0-9.,-]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome da instituição inválido'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if 'nome' in data and data['nome'] != instituicao.nome:
                if Instituicao.objects.filter(nome=data['nome']).exists():
                    logger.warning(f"Instituição com nome {data['nome']} já existe")
                    return Response({'erro': 'Instituição com este nome já existe'}, status=status.HTTP_400_BAD_REQUEST)
                instituicao.nome = data['nome']
            instituicao.descricao = data.get('descricao', instituicao.descricao)
            instituicao.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'admin_global',
                {
                    'type': 'instituicao_atualizada',
                    'instituicao_id': str(instituicao.id),
                    'nome': instituicao.nome,
                    'descricao': instituicao.descricao
                }
            )
            redis_client.delete('cache:search:*')
            logger.info(f"Instituição {instituicao.nome} atualizada por user_id={user.id}")
            return Response({
                'mensagem': 'Instituição atualizada com sucesso',
                'instituicao': {
                    'id': str(instituicao.id),
                    'nome': instituicao.nome,
                    'descricao': instituicao.descricao
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao atualizar instituição {instituicao_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao atualizar instituição'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ExcluirInstituicaoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def delete(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        if user.user_tipo != 'admin_sistema':
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super administradores'}, status=status.HTTP_403_FORBIDDEN)

        instituicao = get_object_or_404(Instituicao, id=instituicao_id)
        if Filial.objects.filter(instituicao_id=instituicao_id).exists():
            logger.warning(f"Tentativa de excluir instituição {instituicao_id} com filiais")
            return Response({'erro': 'Não é possível excluir: instituição possui filiais associadas'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            instituicao.delete()
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'admin_global',
                {
                    'type': 'instituicao_excluida',
                    'instituicao_id': str(instituicao_id),
                    'nome': instituicao.nome
                }
            )
            redis_client.delete('cache:search:*')
            logger.info(f"Instituição {instituicao.nome} excluída por user_id={user.id}")
            return Response({'mensagem': 'Instituição excluída com sucesso'}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao excluir instituição {instituicao_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao excluir instituição'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class CriarFilialView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        instituicao = get_object_or_404(Instituicao, id=instituicao_id)
        data = request.data
        required = ['nome', 'localizacao', 'bairro', 'latitude', 'longitude']
        if not data or not all(f in data for f in required):
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campos obrigatórios faltando: nome, localizacao, bairro, latitude, longitude'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[A-Za-zÀ-ÿ\s0-9.,-]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome da filial inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if not re.match(r'^[A-Za-zÀ-ÿ\s,]{1,100}$', data['bairro']):
            logger.warning(f"Bairro inválido: {data['bairro']}")
            return Response({'erro': 'Bairro inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if not (-90 <= float(data['latitude']) <= 90):
            logger.warning(f"Latitude inválida: {data['latitude']}")
            return Response({'erro': 'Latitude deve estar entre -90 e 90'}, status=status.HTTP_400_BAD_REQUEST)
        if not (-180 <= float(data['longitude']) <= 180):
            logger.warning(f"Longitude inválida: {data['longitude']}")
            return Response({'erro': 'Longitude deve estar entre -180 e 180'}, status=status.HTTP_400_BAD_REQUEST)

        if Filial.objects.filter(instituicao_id=instituicao_id, nome=data['nome']).exists():
            logger.warning(f"Filial com nome {data['nome']} já existe")
            return Response({'erro': 'Filial com este nome já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            filial = Filial(
                id=uuid.uuid4(),
                instituicao_id=instituicao_id,
                nome=data['nome'],
                localizacao=data['localizacao'],
                bairro=data['bairro'],
                latitude=data['latitude'],
                longitude=data['longitude']
            )
            filial.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'filial_criada',
                    'filial_id': str(filial.id),
                    'nome': filial.nome,
                    'localizacao': filial.localizacao,
                    'bairro': filial.bairro,
                    'latitude': filial.latitude,
                    'longitude': filial.longitude,
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(user.id), f"Filial {filial.nome} criada com sucesso na instituição {instituicao.nome}")
            redis_client.delete('cache:search:*')
            logger.info(f"Filial {filial.nome} criada por user_id={user.id}")
            return Response({
                'mensagem': 'Filial criada com sucesso',
                'filial': {
                    'id': str(filial.id),
                    'nome': filial.nome,
                    'localizacao': filial.localizacao,
                    'bairro': filial.bairro,
                    'latitude': filial.latitude,
                    'longitude': filial.longitude,
                    'instituicao_id': str(instituicao_id)
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao criar filial: {str(e)}")
            return Response({'erro': 'Erro interno ao criar filial'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AtualizarFilialView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def put(self, request, instituicao_id, filial_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        instituicao = get_object_or_404(Instituicao, id=instituicao_id)
        filial = get_object_or_404(Filial, id=filial_id, instituicao_id=instituicao_id)
        data = request.data
        if not data:
            logger.warning("Nenhum dado fornecido")
            return Response({'erro': 'Nenhum dado fornecido para atualização'}, status=status.HTTP_400_BAD_REQUEST)

        if 'nome' in data and not re.match(r'^[A-Za-zÀ-ÿ\s0-9.,-]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome da filial inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if 'bairro' in data and not re.match(r'^[A-Za-zÀ-ÿ\s,]{1,100}$', data['bairro']):
            logger.warning(f"Bairro inválido: {data['bairro']}")
            return Response({'erro': 'Bairro inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if 'latitude' in data and not (-90 <= float(data['latitude']) <= 90):
            logger.warning(f"Latitude inválida: {data['latitude']}")
            return Response({'erro': 'Latitude deve estar entre -90 e 90'}, status=status.HTTP_400_BAD_REQUEST)
        if 'longitude' in data and not (-180 <= float(data['longitude']) <= 180):
            logger.warning(f"Longitude inválida: {data['longitude']}")
            return Response({'erro': 'Longitude deve estar entre -180 e 180'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if 'nome' in data and data['nome'] != filial.nome:
                if Filial.objects.filter(instituicao_id=instituicao_id, nome=data['nome']).exists():
                    logger.warning(f"Filial com nome {data['nome']} já existe")
                    return Response({'erro': 'Filial com este nome já existe'}, status=status.HTTP_400_BAD_REQUEST)
                filial.nome = data['nome']
            filial.localizacao = data.get('localizacao', filial.localizacao)
            filial.bairro = data.get('bairro', filial.bairro)
            filial.latitude = data.get('latitude', filial.latitude)
            filial.longitude = data.get('longitude', filial.longitude)
            filial.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'filial_atualizada',
                    'filial_id': str(filial.id),
                    'nome': filial.nome,
                    'localizacao': filial.localizacao,
                    'bairro': filial.bairro,
                    'latitude': filial.latitude,
                    'longitude': filial.longitude,
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(user.id), f"Filial {filial.nome} atualizada na instituição {instituicao.nome}")
            redis_client.delete('cache:search:*')
            logger.info(f"Filial {filial.nome} atualizada por user_id={user.id}")
            return Response({
                'mensagem': 'Filial atualizada com sucesso',
                'filial': {
                    'id': str(filial.id),
                    'nome': filial.nome,
                    'localizacao': filial.localizacao,
                    'bairro': filial.bairro,
                    'latitude': filial.latitude,
                    'longitude': filial.longitude,
                    'instituicao_id': str(instituicao_id)
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao atualizar filial {filial_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao atualizar filial'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarFiliaisView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        get_object_or_404(Instituicao, id=instituicao_id)
        try:
            filiais = Filial.objects.filter(instituicao_id=instituicao_id)
            response = [{
                'id': str(f.id),
                'nome': f.nome,
                'localizacao': f.localizacao,
                'bairro': f.bairro,
                'latitude': f.latitude,
                'longitude': f.longitude,
                'instituicao_id': str(f.instituicao_id)
            } for f in filiais]
            logger.info(f"Admin {user.email} listou {len(response)} filiais da instituição {instituicao_id}")
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao listar filiais para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao listar filiais'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class CriarAdminInstituicaoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        if user.user_tipo != 'admin_sistema':
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super administradores'}, status=status.HTTP_403_FORBIDDEN)

        get_object_or_404(Instituicao, id=instituicao_id)
        data = request.data
        required = ['email', 'nome', 'senha']
        if not data or not all(f in data for f in required):
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campos obrigatórios faltando: email, nome, senha'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', data['email']):
            logger.warning(f"Email inválido: {data['email']}")
            return Response({'erro': 'Email inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if len(data['senha']) < 8:
            logger.warning("Senha muito curta")
            return Response({'erro': 'A senha deve ter pelo menos 8 caracteres'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=data['email']).exists():
            logger.warning(f"Usuário com email {data['email']} já existe")
            return Response({'erro': 'Usuário com este email já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            admin = User(
                id=uuid.uuid4(),
                email=data['email'],
                nome=data['nome'],
                user_tipo='admin_instituicao',
                password=make_password(data['senha']),
                ativo=True
            )
            admin.save()
            perfil = PerfilUsuario(user=admin, instituicao_id=instituicao_id)
            perfil.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'admin_global',
                {
                    'type': 'usuario_criado',
                    'usuario_id': str(admin.id),
                    'email': admin.email,
                    'tipo': admin.user_tipo,
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(admin.id), f"Bem-vindo ao Facilita 2.0 como administrador da instituição")
            logger.info(f"Admin de instituição {admin.email} criado por user_id={user.id}")
            return Response({
                'mensagem': 'Administrador de instituição criado com sucesso',
                'usuario': {
                    'id': str(admin.id),
                    'email': admin.email,
                    'nome': admin.nome,
                    'tipo': admin.user_tipo
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao criar admin de instituição: {str(e)}")
            return Response({'erro': 'Erro interno ao criar administrador'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AtualizarGestorView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def put(self, request, instituicao_id, usuario_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        get_object_or_404(Instituicao, id=instituicao_id)
        target_user = get_object_or_404(User, id=usuario_id)
        target_perfil = get_object_or_404(PerfilUsuario, user=target_user)
        if target_user.user_tipo != 'admin_departamento' or target_perfil.instituicao_id != instituicao_id:
            logger.warning(f"Gestor {usuario_id} não encontrado ou não é admin_departamento")
            return Response({'erro': 'Gestor não encontrado ou não pertence à instituição'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        if not data:
            logger.warning("Nenhum dado fornecido")
            return Response({'erro': 'Nenhum dado fornecido para atualização'}, status=status.HTTP_400_BAD_REQUEST)

        if 'email' in data and not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', data['email']):
            logger.warning(f"Email inválido: {data['email']}")
            return Response({'erro': 'Email inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if 'senha' in data and len(data['senha']) < 8:
            logger.warning("Senha muito curta")
            return Response({'erro': 'A senha deve ter pelo menos 8 caracteres'}, status=status.HTTP_400_BAD_REQUEST)
        if 'nome' in data and not re.match(r'^[A-Za-zÀ-ÿ\s]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome inválido'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if 'email' in data and data['email'] != target_user.email:
                if User.objects.filter(email=data['email']).exists():
                    logger.warning(f"Email {data['email']} já está em uso")
                    return Response({'erro': 'Email já está em uso'}, status=status.HTTP_400_BAD_REQUEST)
                target_user.email = data['email']
            target_user.nome = data.get('nome', target_user.nome)
            if 'senha' in data:
                target_user.password = make_password(data['senha'])
            if 'departamento_id' in data:
                departamento = get_object_or_404(Departamento, id=data['departamento_id'])
                if departamento.instituicao_id != instituicao_id:
                    logger.warning(f"Departamento {data['departamento_id']} inválido")
                    return Response({'erro': 'Departamento inválido'}, status=status.HTTP_400_BAD_REQUEST)
                target_perfil.departamento_id = data['departamento_id']
            if 'filial_id' in data:
                filial = get_object_or_404(Filial, id=data['filial_id'])
                if filial.instituicao_id != instituicao_id:
                    logger.warning(f"Filial {data['filial_id']} inválida")
                    return Response({'erro': 'Filial inválida'}, status=status.HTTP_400_BAD_REQUEST)
                target_perfil.filial_id = data['filial_id']
            target_user.save()
            target_perfil.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'usuario_atualizado',
                    'usuario_id': str(target_user.id),
                    'email': target_user.email,
                    'nome': target_user.nome,
                    'departamento_id': str(target_perfil.departamento_id) if target_perfil.departamento_id else None,
                    'filial_id': str(target_perfil.filial_id) if target_perfil.filial_id else None,
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(target_user.id), f"Seus dados foram atualizados na instituição")
            logger.info(f"Gestor {target_user.email} atualizado por user_id={user.id}")
            return Response({
                'mensagem': 'Gestor atualizado com sucesso',
                'usuario': {
                    'id': str(target_user.id),
                    'email': target_user.email,
                    'nome': target_user.nome,
                    'tipo': target_user.user_tipo,
                    'departamento_id': str(target_perfil.departamento_id) if target_perfil.departamento_id else None,
                    'filial_id': str(target_perfil.filial_id) if target_perfil.filial_id else None
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao atualizar gestor {usuario_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao atualizar gestor'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ExcluirGestorView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def delete(self, request, instituicao_id, usuario_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        get_object_or_404(Instituicao, id=instituicao_id)
        target_user = get_object_or_404(User, id=usuario_id)
        target_perfil = get_object_or_404(PerfilUsuario, user=target_user)
        if target_user.user_tipo != 'admin_departamento' or target_perfil.instituicao_id != instituicao_id:
            logger.warning(f"Gestor {usuario_id} não encontrado ou não é admin_departamento")
            return Response({'erro': 'Gestor não encontrado ou não pertence à instituição'}, status=status.HTTP_404_NOT_FOUND)

        if target_perfil.departamento_id:
            filas = Fila.objects.filter(departamento_id=target_perfil.departamento_id)
            for fila in filas:
                if Ticket.objects.filter(fila_id=fila.id, status='Pendente').exists():
                    logger.warning(f"Gestor {usuario_id} tem tickets pendentes")
                    return Response({'erro': 'Não é possível excluir: gestor tem filas com tickets pendentes'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_user.delete()
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'usuario_excluido',
                    'usuario_id': str(usuario_id),
                    'email': target_user.email,
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(target_user.id), f"Sua conta foi removida da instituição")
            logger.info(f"Gestor {target_user.email} excluído por user_id={user.id}")
            return Response({'mensagem': 'Gestor excluído com sucesso'}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao excluir gestor {usuario_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao excluir gestor'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class CriarDepartamentoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        get_object_or_404(Instituicao, id=instituicao_id)
        data = request.data
        required = ['nome', 'setor', 'filial_id']
        if not data or not all(f in data for f in required):
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campos obrigatórios faltando: nome, setor, filial_id'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[A-Za-zÀ-ÿ\s0-9.,-]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome do departamento inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if not re.match(r'^[A-Za-zÀ-ÿ\s]{1,50}$', data['setor']):
            logger.warning(f"Setor inválido: {data['setor']}")
            return Response({'erro': 'Setor inválido'}, status=status.HTTP_400_BAD_REQUEST)
        filial = get_object_or_404(Filial, id=data['filial_id'])
        if filial.instituicao_id != instituicao_id:
            logger.warning(f"Filial {data['filial_id']} inválida")
            return Response({'erro': 'Filial inválida'}, status=status.HTTP_400_BAD_REQUEST)

        if Departamento.objects.filter(instituicao_id=instituicao_id, nome=data['nome'], filial_id=data['filial_id']).exists():
            logger.warning(f"Departamento com nome {data['nome']} já existe")
            return Response({'erro': 'Departamento com este nome já existe na filial'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            departamento = Departamento(
                id=uuid.uuid4(),
                instituicao_id=instituicao_id,
                filial_id=data['filial_id'],
                nome=data['nome'],
                setor=data['setor']
            )
            departamento.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'departamento_criado',
                    'departamento_id': str(departamento.id),
                    'nome': departamento.nome,
                    'setor': departamento.setor,
                    'filial_id': str(departamento.filial_id),
                    'instituicao_id': str(instituicao_id)
                }
            )
            redis_client.delete('cache:search:*')
            logger.info(f"Departamento {departamento.nome} criado por user_id={user.id}")
            return Response({
                'mensagem': 'Departamento criado com sucesso',
                'departamento': {
                    'id': str(departamento.id),
                    'nome': departamento.nome,
                    'setor': departamento.setor,
                    'filial_id': str(departamento.filial_id),
                    'instituicao_id': str(instituicao_id)
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao criar departamento: {str(e)}")
            return Response({'erro': 'Erro interno ao criar departamento'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AdicionarUsuarioDepartamentoView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, departamento_id):
        user = User.objects.get(id=request.user.id)
        departamento = get_object_or_404(Departamento, id=departamento_id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == departamento.instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        required = ['email', 'nome', 'senha', 'tipo']
        if not data or not all(f in data for f in required):
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campos obrigatórios faltando: email, nome, senha, tipo'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', data['email']):
            logger.warning(f"Email inválido: {data['email']}")
            return Response({'erro': 'Email inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if len(data['senha']) < 8:
            logger.warning("Senha muito curta")
            return Response({'erro': 'A senha deve ter pelo menos 8 caracteres'}, status=status.HTTP_400_BAD_REQUEST)
        if not re.match(r'^[A-Za-zÀ-ÿ\s]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome inválido'}, status=status.HTTP_400_BAD_REQUEST)

        tipo = data['tipo'].upper()
        if tipo not in ['USUARIO', 'ADMIN_DEPARTAMENTO']:
            logger.warning(f"Tipo inválido: {tipo}")
            return Response({'erro': 'Tipo deve ser USUARIO ou ADMIN_DEPARTAMENTO'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=data['email']).exists():
            logger.warning(f"Usuário com email {data['email']} já existe")
            return Response({'erro': 'Usuário com este email já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            new_user = User(
                id=uuid.uuid4(),
                email=data['email'],
                nome=data['nome'],
                user_tipo='admin_departamento' if tipo == 'ADMIN_DEPARTAMENTO' else 'usuario',
                password=make_password(data['senha']),
                ativo=True
            )
            new_user.save()
            perfil = PerfilUsuario(
                user=new_user,
                instituicao_id=departamento.instituicao_id,
                departamento_id=departamento_id,
                filial_id=departamento.filial_id
            )
            perfil.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{departamento.instituicao_id}',
                {
                    'type': 'usuario_criado',
                    'usuario_id': str(new_user.id),
                    'email': new_user.email,
                    'tipo': new_user.user_tipo,
                    'departamento_id': str(departamento_id),
                    'filial_id': str(departamento.filial_id),
                    'instituicao_id': str(departamento.instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(new_user.id), f"Bem-vindo ao Facilita 2.0 no departamento {departamento.nome}")
            logger.info(f"Usuário {new_user.email} ({tipo}) adicionado ao departamento {departamento.nome}")
            return Response({
                'mensagem': 'Usuário adicionado ao departamento com sucesso',
                'usuario': {
                    'id': str(new_user.id),
                    'email': new_user.email,
                    'nome': new_user.nome,
                    'tipo': new_user.user_tipo,
                    'departamento_id': str(departamento_id),
                    'filial_id': str(departamento.filial_id)
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao adicionar usuário ao departamento: {str(e)}")
            return Response({'erro': 'Erro interno ao adicionar usuário'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarChamadasInstituicaoView(APIView):
    def get(self, request, instituicao_id):
        instituicao = get_object_or_404(Instituicao, id=instituicao_id)
        cache_key = f'chamadas:{instituicao_id}'
        refresh = request.query_params.get('refresh', 'false').lower() == 'true'

        if not refresh:
            try:
                cached_data = redis_client.get(cache_key)
                if cached_data:
                    return Response(json.loads(cached_data), status=status.HTTP_200_OK)
            except Exception as e:
                logger.warning(f"Erro ao acessar Redis: {str(e)}")

        try:
            filiais = Filial.objects.filter(instituicao_id=instituicao_id)
            filial_ids = [f.id for f in filiais]
            departamentos = Departamento.objects.filter(filial_id__in=filial_ids)
            departamento_ids = [d.id for d in departamentos]
            filas = Fila.objects.filter(departamento_id__in=departamento_ids)
            fila_ids = [q.id for q in filas]

            chamadas_recentes = Ticket.objects.filter(
                fila_id__in=fila_ids,
                status__in=['Chamado', 'Atendido']
            ).order_by('-atendido_em', '-emitido_em')[:10]

            response = []
            for ticket in chamadas_recentes:
                fila = ticket.fila
                chamada = {
                    'senha_id': str(ticket.id),
                    'numero_senha': f"{fila.prefixo}{ticket.numero_senha}",
                    'servico': fila.servico,
                    'departamento': fila.departamento.nome,
                    'filial': fila.departamento.filial.nome if fila.departamento.filial else 'N/A',
                    'guiche': f"Guichê {ticket.guiche:02d}" if ticket.guiche else 'N/A',
                    'status': ticket.status,
                    'chamado_em': ticket.atendido_em.isoformat() if ticket.atendido_em else ticket.emitido_em.isoformat()
                }
                response.append(chamada)
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f'painel_{instituicao_id}',
                    {
                        'type': 'status_chamada',
                        'instituicao_id': str(instituicao_id),
                        'fila_id': str(fila.id),
                        'evento': 'status_chamada',
                        'dados': chamada
                    }
                )

            response_data = {
                'instituicao_id': str(instituicao_id),
                'nome_instituicao': instituicao.nome,
                'chamadas': response
            }

            try:
                redis_client.setex(cache_key, 300, json.dumps(response_data))
            except Exception as e:
                logger.warning(f"Erro ao salvar cache no Redis: {str(e)}")

            logger.info(f"Listadas {len(response)} chamadas para instituição {instituicao_id}")
            return Response(response_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao listar chamadas para instituição {instituicao_id}: {str(e)}")
            return Response({'erro': 'Erro interno ao listar chamadas'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarFilasAdminView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if user.user_tipo not in ['admin_sistema', 'admin_instituicao', 'admin_departamento']:
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a administradores'}, status=status.HTTP_403_FORBIDDEN)

        try:
            now = datetime.now(pytz.UTC)
            current_weekday = now.strftime('%A').upper()
            current_time = now.time()

            if user.user_tipo == 'admin_sistema':
                filas = Fila.objects.all()
            elif user.user_tipo == 'admin_instituicao':
                filiais = Filial.objects.filter(instituicao_id=perfil.instituicao_id)
                filial_ids = [f.id for f in filiais]
                departamento_ids = [d.id for d in Departamento.objects.filter(filial_id__in=filial_ids)]
                filas = Fila.objects.filter(departamento_id__in=departamento_ids)
            else:
                if not perfil.departamento_id:
                    logger.warning(f"Gestor {request.user.id} não vinculado a departamento")
                    return Response({'erro': 'Gestor não vinculado a um departamento'}, status=status.HTTP_403_FORBIDDEN)
                filas = Fila.objects.filter(departamento_id=perfil.departamento_id)

            response = []
            for fila in filas:
                horario = HorarioFila.objects.filter(fila_id=fila.id, dia_semana=current_weekday).first()
                aberta = False
                if horario and not horario.fechado:
                    aberta = (
                        horario.hora_abertura and horario.hora_fechamento and
                        current_time >= horario.hora_abertura and
                        current_time <= horario.hora_fechamento and
                        fila.tickets_ativos < fila.limite_diario
                    )
                tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, fila.senha_atual + 1, 0)
                response.append({
                    'id': str(fila.id),
                    'servico': fila.servico,
                    'prefixo': fila.prefixo,
                    'nome_instituicao': fila.departamento.instituicao.nome if fila.departamento and fila.departamento.instituicao else 'N/A',
                    'nome_filial': fila.departamento.filial.nome if fila.departamento.filial else 'N/A',
                    'tickets_ativos': fila.tickets_ativos,
                    'limite_diario': fila.limite_diario,
                    'senha_atual': fila.senha_atual,
                    'status': 'Aberta' if aberta else ('Lotada' if fila.tickets_ativos >= fila.limite_diario else 'Fechada'),
                    'instituicao_id': str(fila.departamento.instituicao_id) if fila.departamento else None,
                    'departamento': fila.departamento.nome if fila.departamento else 'N/A',
                    'filial_id': str(fila.departamento.filial_id),
                    'hora_abertura': horario.hora_abertura.strftime('%H:%M') if horario and horario.hora_abertura else None,
                    'hora_fechamento': horario.hora_fechamento.strftime('%H:%M') if horario and horario.hora_fechamento else None,
                    'tempo_espera_medio': f"{int(tempo_espera)} minutos" if tempo_espera is not None else 'N/A'
                })

            logger.info(f"Usuário {user.email} listou {len(response)} filas")
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao listar filas para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao listar filas'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ChamarProximaSenhaAdminView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, fila_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if user.user_tipo not in ['admin_sistema', 'admin_instituicao', 'admin_departamento']:
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a administradores'}, status=status.HTTP_403_FORBIDDEN)

        fila = get_object_or_404(Fila, id=fila_id)
        if user.user_tipo == 'admin_departamento' and fila.departamento_id != perfil.departamento_id:
            logger.warning(f"Gestor {request.user.id} tentou acessar fila fora de seu departamento")
            return Response({'erro': 'Acesso negado: fila não pertence ao seu departamento'}, status=status.HTTP_403_FORBIDDEN)
        if user.user_tipo == 'admin_instituicao' and fila.departamento.instituicao_id != perfil.instituicao_id:
            logger.warning(f"Admin {request.user.id} tentou acessar fila fora de sua instituição")
            return Response({'erro': 'Acesso negado: fila não pertence à sua instituição'}, status=status.HTTP_403_FORBIDDEN)

        try:
            senha = ServicoFila.chamar_proximo(fila.servico)
            response = {
                'mensagem': f'Senha {senha.fila.prefixo}{senha.numero_senha} chamada',
                'senha_id': str(senha.id),
                'numero_senha': f"{senha.fila.prefixo}{senha.numero_senha}",
                'guiche': senha.guiche,
                'restantes': senha.fila.tickets_ativos
            }
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'departamento_{fila.departamento_id}',
                {
                    'type': 'notificacao',
                    'mensagem': f"Senha {senha.fila.prefixo}{senha.numero_senha} chamada no guichê {senha.guiche:02d}",
                    'departamento_id': str(fila.departamento_id)
                }
            )
            async_to_sync(channel_layer.group_send)(
                f'painel_{fila.departamento.instituicao_id}',
                {
                    'type': 'nova_chamada',
                    'instituicao_id': str(fila.departamento.instituicao_id),
                    'fila_id': str(fila.id),
                    'evento': 'nova_chamada',
                    'dados': {
                        'numero_senha': f"{fila.prefixo}{senha.numero_senha}",
                        'guiche': senha.guiche,
                        'timestamp': senha.atendido_em.isoformat() if senha.atendido_em else datetime.now(pytz.UTC).isoformat()
                    }
                }
            )
            ServicoFila.send_fcm_notification(str(senha.user_id), f"Senha {senha.fila.prefixo}{senha.numero_senha} chamada no guichê {senha.guiche:02d}")
            logger.info(f"Usuário {user.email} chamou senha {senha.id} da fila {fila_id}")
            return Response(response, status=status.HTTP_200_OK)
        except ValueError as e:
            logger.error(f"Erro ao chamar próxima senha na fila {fila_id}: {str(e)}")
            return Response({'erro': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Erro interno ao chamar próxima senha: {str(e)}")
            return Response({'erro': 'Erro interno ao chamar senha'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RelatorioAdminView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if user.user_tipo not in ['admin_sistema', 'admin_instituicao', 'admin_departamento']:
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a administradores'}, status=status.HTTP_403_FORBIDDEN)

        data_str = request.query_params.get('data')
        try:
            data_relatorio = datetime.strptime(data_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            logger.warning(f"Data inválida: {data_str}")
            return Response({'erro': 'Data inválida. Use o formato AAAA-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if user.user_tipo == 'admin_sistema':
                filas = Fila.objects.all()
            elif user.user_tipo == 'admin_instituicao':
                filiais = Filial.objects.filter(instituicao_id=perfil.instituicao_id)
                filial_ids = [f.id for f in filiais]
                departamento_ids = [d.id for d in Departamento.objects.filter(filial_id__in=filial_ids)]
                filas = Fila.objects.filter(departamento_id__in=departamento_ids)
            else:
                if not perfil.departamento_id:
                    logger.warning(f"Gestor {request.user.id} não vinculado a departamento")
                    return Response({'erro': 'Gestor não vinculado a um departamento'}, status=status.HTTP_403_FORBIDDEN)
                filas = Fila.objects.filter(departamento_id=perfil.departamento_id)

            fila_ids = [q.id for q in filas]
            inicio = datetime.combine(data_relatorio, datetime.min.time(), tzinfo=pytz.UTC)
            fim = inicio + timedelta(days=1)

            dia_semana = data_relatorio.strftime('%A').upper()
            relatorio = []
            for fila in filas:
                horario = HorarioFila.objects.filter(fila_id=fila.id, dia_semana=dia_semana).first()
                if horario and horario.fechado:
                    continue

                tickets = Ticket.objects.filter(
                    fila_id=fila.id,
                    emitido_em__gte=inicio,
                    emitido_em__lt=fim
                )
                emitidas = tickets.count()
                atendidas = tickets.filter(status='Atendido').count()
                tempos = [
                    ServicoFila.calcular_tempo_espera(fila.id, t.numero_senha, t.prioridade)
                    for t in tickets.filter(status='Atendido', atendido_em__isnull=False, emitido_em__isnull=False)
                ]
                tempo_medio = sum(tempos) / len(tempos) if tempos else None

                relatorio.append({
                    'servico': fila.servico,
                    'filial': fila.departamento.filial.nome if fila.departamento.filial else 'N/A',
                    'emitidas': emitidas,
                    'atendidas': atendidas,
                    'tempo_medio': round(tempo_medio, 2) if tempo_medio else None,
                })

            logger.info(f"Relatório gerado para {user.email} em {data_str}: {len(relatorio)} serviços")
            return Response(relatorio, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao gerar relatório para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao gerar relatório'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarDepartamentosView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        try:
            filiais = Filial.objects.filter(instituicao_id=instituicao_id)
            filial_ids = [f.id for f in filiais]
            departamentos = Departamento.objects.filter(filial_id__in=filial_ids)
            response = [{
                'id': str(d.id),
                'nome': d.nome,
                'setor': d.setor,
                'filial_id': str(d.filial_id),
                'nome_filial': d.filial.nome if d.filial else 'N/A'
            } for d in departamentos]
            logger.info(f"Admin {user.email} listou {len(response)} departamentos da instituição {instituicao_id}")
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao listar departamentos para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao listar departamentos'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ListarGestoresView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        try:
            gestores = User.objects.filter(user_tipo='admin_departamento')
            perfis = PerfilUsuario.objects.filter(user__in=gestores, instituicao_id=instituicao_id)
            response = [{
                'id': str(p.user.id),
                'email': p.user.email,
                'nome': p.user.nome,
                'departamento_id': str(p.departamento_id) if p.departamento_id else 'N/A',
                'nome_departamento': p.departamento.nome if p.departamento else 'N/A',
                'filial_id': str(p.filial_id) if p.filial_id else 'N/A',
                'nome_filial': p.filial.nome if p.filial else 'N/A'
            } for p in perfis]
            logger.info(f"Admin {user.email} listou {len(response)} gestores da instituição {instituicao_id}")
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao listar gestores para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao listar gestores'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class CriarGestorView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def post(self, request, instituicao_id):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        if not (user.user_tipo == 'admin_sistema' or (user.user_tipo == 'admin_instituicao' and perfil.instituicao_id == instituicao_id)):
            logger.warning(f"Tentativa não autorizada por user_id={request.user.id}")
            return Response({'erro': 'Acesso restrito a super admins ou admins da instituição'}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        required = ['email', 'nome', 'senha', 'departamento_id', 'filial_id']
        if not data or not all(f in data for f in required):
            logger.warning("Campos obrigatórios faltando")
            return Response({'erro': 'Campos obrigatórios faltando: email, nome, senha, departamento_id, filial_id'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', data['email']):
            logger.warning(f"Email inválido: {data['email']}")
            return Response({'erro': 'Email inválido'}, status=status.HTTP_400_BAD_REQUEST)
        if len(data['senha']) < 8:
            logger.warning("Senha muito curta")
            return Response({'erro': 'A senha deve ter pelo menos 8 caracteres'}, status=status.HTTP_400_BAD_REQUEST)
        if not re.match(r'^[A-Za-zÀ-ÿ\s]{1,100}$', data['nome']):
            logger.warning(f"Nome inválido: {data['nome']}")
            return Response({'erro': 'Nome inválido'}, status=status.HTTP_400_BAD_REQUEST)

        departamento = get_object_or_404(Departamento, id=data['departamento_id'])
        if departamento.instituicao_id != instituicao_id:
            logger.warning(f"Departamento {data['departamento_id']} inválido")
            return Response({'erro': 'Departamento inválido'}, status=status.HTTP_400_BAD_REQUEST)

        filial = get_object_or_404(Filial, id=data['filial_id'])
        if filial.instituicao_id != instituicao_id:
            logger.warning(f"Filial {data['filial_id']} inválida")
            return Response({'erro': 'Filial inválida'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=data['email']).exists():
            logger.warning(f"Usuário com email {data['email']} já existe")
            return Response({'erro': 'Usuário com este email já existe'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            gestor = User(
                id=uuid.uuid4(),
                email=data['email'],
                nome=data['nome'],
                user_tipo='admin_departamento',
                password=make_password(data['senha']),
                ativo=True
            )
            gestor.save()
            perfil = PerfilUsuario(
                user=gestor,
                instituicao_id=instituicao_id,
                departamento_id=data['departamento_id'],
                filial_id=data['filial_id']
            )
            perfil.save()

            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f'admin_{instituicao_id}',
                {
                    'type': 'usuario_criado',
                    'usuario_id': str(gestor.id),
                    'email': gestor.email,
                    'nome': gestor.nome,
                    'departamento_id': str(data['departamento_id']),
                    'filial_id': str(data['filial_id']),
                    'instituicao_id': str(instituicao_id)
                }
            )
            ServicoFila.send_fcm_notification(str(gestor.id), f"Bem-vindo ao Facilita 2.0 como gestor do departamento {departamento.nome}")
            logger.info(f"Gestor {gestor.email} criado por user_id={user.id}")
            return Response({
                'mensagem': 'Gestor criado com sucesso',
                'usuario': {
                    'id': str(gestor.id),
                    'email': gestor.email,
                    'nome': gestor.nome,
                    'departamento_id': str(data['departamento_id']),
                    'nome_departamento': departamento.nome,
                    'filial_id': str(data['filial_id']),
                    'nome_filial': filial.nome
                }
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Erro ao criar gestor: {str(e)}")
            return Response({'erro': 'Erro interno ao criar gestor'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class InformacoesUsuarioView(APIView):
    authentication_classes = [FirebaseAndTokenAuthentication]

    def get(self, request):
        user = User.objects.get(id=request.user.id)
        perfil = PerfilUsuario.objects.get(user=user)
        try:
            response = {
                'id': str(user.id),
                'email': user.email,
                'nome': user.nome,
                'tipo_usuario': user.user_tipo,
                'instituicao_id': str(perfil.instituicao_id) if perfil.instituicao_id else None,
                'departamento_id': str(perfil.departamento_id) if perfil.departamento_id else None,
                'filial_id': str(perfil.filial_id) if perfil.filial_id else None,
                'nome_departamento': perfil.departamento.nome if perfil.departamento else None,
                'nome_filial': perfil.filial.nome if perfil.filial else None
            }
            ServicoFila.verificar_notificacoes_proativas(str(user.id))
            logger.info(f"Informações do usuário retornadas para user_id={user.id}")
            return Response(response, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Erro ao buscar informações do usuário para user_id={user.id}: {str(e)}")
            return Response({'erro': 'Erro interno ao buscar informações do usuário'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)