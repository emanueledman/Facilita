import logging
import uuid
import numpy as np
from django.utils import timezone
from datetime import datetime, timedelta
from django.db.models import Q, Max
from django.conf import settings
from geopy.distance import geodesic
import redis
import json
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from firebase_admin import messaging
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from fila_online.models import Fila, HorarioFila, Ticket, Departamento, Categoria, EtiquetaServico
from sistema.models import PerfilUsuario, PreferenciaUsuario, LogAuditoria, Instituicao, Filial
from .ml_models import preditor_tempo_espera, preditor_recomendacao_servico
from .utils.pdf_generator import gerar_pdf_senha  # Assumindo que o gerador de PDF foi renomeado

logger = logging.getLogger(__name__)

# Configuração do Redis
redis_client = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=0, decode_responses=True)

class ServicoFila:
    MINUTOS_EXPIRACAO_PADRAO = 30
    MINUTOS_TIMEOUT_CHAMADA = 5
    LIMITE_PROXIMIDADE_KM = 1.0
    LIMITE_PROXIMIDADE_PRESENCA_KM = 0.5

    @staticmethod
    def gerar_codigo_qr():
        codigo_qr = f"QR-{uuid.uuid4().hex[:8]}"
        logger.debug(f"Código QR gerado: {codigo_qr}")
        return codigo_qr

    @staticmethod
    def gerar_comprovante(senha):
        fila = senha.fila
        if not fila or not fila.departamento or not fila.departamento.filial or not fila.departamento.filial.instituicao:
            logger.error(f"Dados incompletos para a senha {senha.id}: fila, departamento, filial ou instituição ausentes")
            raise ValueError("Fila, departamento ou instituição associada à senha não encontrada")

        comprovante = (
            "=== Comprovante Facilita 2.0 ===\n"
            f"Serviço: {fila.servico}\n"
            f"Instituição: {fila.departamento.filial.instituicao.nome}\n"
            f"Filial: {fila.departamento.filial.nome}\n"
            f"Bairro: {fila.departamento.filial.bairro or 'Não especificado'}\n"
            f"Senha: {fila.prefixo}{senha.numero_ticket}\n"
            f"Tipo: {'Física' if senha.e_fisico else 'Virtual'}\n"
            f"Código QR: {senha.codigo_qr}\n"
            f"Prioridade: {senha.prioridade}\n"
            f"Data de Emissão: {senha.emitido_em.strftime('%d/%m/%Y %H:%M')}\n"
            f"Expira em: {senha.expira_em.strftime('%d/%m/%Y %H:%M') if senha.expira_em else 'N/A'}\n"
            "=== Guarde este comprovante ==="
        )
        logger.debug(f"Comprovante gerado para senha {senha.id}")
        return comprovante

    @staticmethod
    def gerar_pdf_senha(senha, posicao=None, tempo_espera=None):
        if not senha.fila or not senha.fila.departamento or not senha.fila.departamento.filial or not senha.fila.departamento.filial.instituicao:
            logger.error(f"Dados incompletos para a senha {senha.id}: fila, departamento, filial ou instituição ausentes")
            raise ValueError("Fila, departamento ou instituição associada à senha não encontrada")

        if posicao is None:
            posicao = max(0, senha.numero_ticket - senha.fila.ticket_atual)
        if tempo_espera is None:
            tempo_espera = ServicoFila.calcular_tempo_espera(
                senha.fila.id, senha.numero_ticket, senha.prioridade
            )

        pdf_buffer = gerar_pdf_senha(
            senha=senha,
            nome_instituicao=f"{senha.fila.departamento.filial.instituicao.nome} - {senha.fila.departamento.filial.nome}",
            servico=senha.fila.servico,
            posicao=posicao,
            tempo_espera=tempo_espera
        )
        return pdf_buffer

    @staticmethod
    def calcular_tempo_espera(fila_id, numero_senha, prioridade=0):
        try:
            fila = Fila.objects.get(id=fila_id)
        except ObjectDoesNotExist:
            logger.error(f"Fila não encontrada para fila_id={fila_id}")
            return 0

        if not ServicoFila.esta_fila_aberta(fila):
            logger.warning(f"Fila {fila_id} está fechada para cálculo de tempo_espera")
            return "N/A"

        if fila.ticket_atual == 0:
            fila.ticket_atual = 1
            fila.save()
            logger.debug(f"Atendimento ainda não começou para fila_id={fila_id}, inicializando ticket_atual=1")

        posicao = max(0, numero_senha - fila.ticket_atual)
        if posicao == 0:
            logger.debug(f"Senha {numero_senha} está na posição 0, tempo_espera=0")
            return 0

        agora = timezone.now()
        hora_do_dia = agora.hour
        tempo_previsto = preditor_tempo_espera.prever(fila_id, posicao, fila.tickets_ativos, prioridade, hora_do_dia)

        if tempo_previsto is not None:
            tempo_espera = tempo_previsto
        else:
            senhas_concluidas = Ticket.objects.filter(fila_id=fila_id, status='Atendido')
            tempos_servico = [s.tempo_servico for s in senhas_concluidas if s.tempo_servico is not None and s.tempo_servico > 0]

            if tempos_servico:
                tempo_medio = np.mean(tempos_servico)
                tempo_estimado = tempo_medio
                logger.debug(f"Tempo médio de atendimento calculado: {tempo_medio} min")
            else:
                tempo_estimado = fila.tempo_espera_medio or 5
                logger.debug(f"Nenhuma senha atendida, usando tempo padrão: {tempo_estimado} min")

            tempo_espera = posicao * tempo_estimado
            if prioridade > 0:
                tempo_espera *= (1 - prioridade * 0.1)
            if fila.tickets_ativos > 10:
                tempo_espera += (fila.tickets_ativos - 10) * 0.5

            fila.tempo_espera_medio = tempo_estimado
            fila.save()

        tempo_espera = round(tempo_espera, 1)
        logger.debug(f"Tempo de espera calculado para senha {numero_senha} na fila {fila_id}: {tempo_espera} min (posicao={posicao}, prioridade={prioridade})")
        return tempo_espera

    @staticmethod
    def calcular_distancia(lat_usuario, lon_usuario, filial):
        if not all([lat_usuario, lon_usuario, filial.latitude, filial.longitude]):
            logger.warning(f"Coordenadas incompletas para cálculo de distância: lat_usuario={lat_usuario}, lon_usuario={lon_usuario}, lat_filial={filial.latitude}, lon_filial={filial.longitude}")
            return None

        local_usuario = (lat_usuario, lon_usuario)
        local_filial = (filial.latitude, filial.longitude)
        try:
            distancia = geodesic(local_usuario, local_filial).kilometers
            return round(distancia, 2)
        except Exception as e:
            logger.error(f"Erro ao calcular distância: {e}")
            return None

    @staticmethod
    def enviar_notificacao(token_fcm, mensagem, senha_id=None, via_websocket=False, usuario_id=None):
        logger.info(f"Notificação para usuario_id {usuario_id}: {mensagem}")

        if not token_fcm and usuario_id:
            try:
                perfil = PerfilUsuario.objects.get(usuario_id=usuario_id)
                if perfil.token_fcm:
                    token_fcm = perfil.token_fcm
                    logger.debug(f"Token FCM recuperado do banco para usuario_id {usuario_id}: {token_fcm}")
                else:
                    logger.warning(f"Token FCM não encontrado para usuario_id {usuario_id}")
            except ObjectDoesNotExist:
                logger.warning(f"Perfil não encontrado para usuario_id {usuario_id}")

        if token_fcm:
            try:
                mensagem_fcm = messaging.Message(
                    notification=messaging.Notification(
                        title="Facilita 2.0",
                        body=mensagem
                    ),
                    data={"senha_id": str(senha_id) or ""},
                    token=token_fcm
                )
                resposta = messaging.send(mensagem_fcm)
                logger.info(f"Notificação FCM enviada para {token_fcm}: {resposta}")
            except Exception as e:
                logger.error(f"Erro ao enviar notificação FCM: {e}")

        if via_websocket and usuario_id:
            try:
                camada_canal = get_channel_layer()
                async_to_sync(camada_canal.group_send)(
                    f"usuario_{usuario_id}",
                    {
                        "type": "notificacao",
                        "mensagem": {"usuario_id": str(usuario_id), "mensagem": mensagem}
                    }
                )
                logger.debug(f"Notificação WebSocket enviada para usuario_id={usuario_id}")
            except Exception as e:
                logger.error(f"Erro ao enviar notificação via WebSocket: {e}")

    @staticmethod
    @transaction.atomic
    def adicionar_a_fila(servico, usuario_id, prioridade=0, e_fisico=False, token_fcm=None, filial_id=None):
        consulta = Fila.objects.filter(servico=servico)
        if filial_id:
            consulta = consulta.filter(departamento__filial__id=filial_id)
        fila = consulta.first()

        if not fila:
            logger.error(f"Fila não encontrada para o serviço: {servico} e filial_id: {filial_id}")
            raise ValueError("Fila não encontrada")

        if not ServicoFila.esta_fila_aberta(fila):
            logger.warning(f"Fila {fila.id} está fechada para emissão de senha")
            raise ValueError(f"A fila {fila.servico} está fechada no momento.")

        if fila.tickets_ativos >= fila.limite_diario:
            logger.warning(f"Fila {servico} está cheia: {fila.tickets_ativos}/{fila.limite_diario}")
            raise ValueError("Limite diário atingido")

        if Ticket.objects.filter(usuario_id=usuario_id, fila_id=fila.id, status='Pendente').exists():
            logger.warning(f"Usuário {usuario_id} já possui uma senha ativa na fila {fila.id}")
            raise ValueError("Você já possui uma senha ativa")

        numero_senha = fila.tickets_ativos + 1
        codigo_qr = ServicoFila.gerar_codigo_qr()
        senha = Ticket(
            id=uuid.uuid4(),
            fila=fila,
            usuario_id=usuario_id,
            numero_ticket=numero_senha,
            codigo_qr=codigo_qr,
            prioridade=prioridade,
            e_fisico=e_fisico,
            emitido_em=timezone.now()
        )

        fila.tickets_ativos += 1
        fila.save()
        senha.save()

        senha_completa = Ticket.objects.get(id=senha.id)  # Recarregar para garantir relações
        if not senha_completa.fila:
            logger.error(f"Relação senha.fila não carregada para senha {senha.id}")
            raise ValueError("Erro ao carregar a fila associada à senha")

        senha.dados_recibo = ServicoFila.gerar_comprovante(senha) if e_fisico else None
        tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, numero_senha, prioridade)
        posicao = max(0, senha.numero_ticket - fila.ticket_atual)
        pdf_buffer = None

        if e_fisico:
            pdf_buffer = ServicoFila.gerar_pdf_senha(senha, posicao, tempo_espera)

        mensagem = f"Senha {fila.prefixo}{numero_senha} emitida. QR: {codigo_qr}. Espera: {tempo_espera if tempo_espera != 'N/A' else 'Aguardando início'}"
        ServicoFila.enviar_notificacao(token_fcm, mensagem, senha.id, via_websocket=True, usuario_id=usuario_id)

        try:
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"fila_{fila.id}",
                {
                    "type": "atualizacao_fila",
                    "mensagem": {
                        "fila_id": str(fila.id),
                        "tickets_ativos": fila.tickets_ativos,
                        "ticket_atual": fila.ticket_atual,
                        "mensagem": f"Nova senha emitida: {fila.prefixo}{numero_senha}"
                    }
                }
            )
        except Exception as e:
            logger.error(f"Erro ao enviar atualização de fila via WebSocket: {e}")

        logger.info(f"Senha {senha.id} adicionada à fila {servico}")
        return senha, pdf_buffer

    @staticmethod
    @transaction.atomic
    def gerar_senha_fisica_para_totem(fila_id, ip_cliente):
        try:
            fila = Fila.objects.get(id=fila_id)
        except ObjectDoesNotExist:
            logger.error(f"Fila não encontrada para fila_id={fila_id}")
            raise ValueError("Fila não encontrada")

        if not ServicoFila.esta_fila_aberta(fila):
            logger.warning(f"Fila {fila_id} está fechada")
            raise ValueError("Fila está fechada no momento")

        if fila.tickets_ativos >= fila.limite_diario:
            logger.warning(f"Fila {fila_id} atingiu o limite diário: {fila.tickets_ativos}/{fila.limite_diario}")
            raise ValueError("Limite diário de senhas atingido")

        chave_cache = f'limite_senha:{ip_cliente}:{fila.departamento.filial.instituicao_id}'
        try:
            contagem_emissao = redis_client.get(chave_cache)
            contagem_emissao = int(contagem_emissao) if contagem_emissao else 0
            if contagem_emissao >= 5:
                logger.warning(f"Limite de emissões atingido para IP {ip_cliente}")
                raise ValueError("Limite de emissões por hora atingido. Tente novamente mais tarde.")
            redis_client.setex(chave_cache, 3600, contagem_emissao + 1)
        except Exception as e:
            logger.warning(f"Erro ao acessar Redis para limite de emissão ({ip_cliente}): {e}. Prosseguindo sem limite.")

        numero_senha = fila.tickets_ativos + 1
        codigo_qr = ServicoFila.gerar_codigo_qr()
        senha = Ticket(
            id=uuid.uuid4(),
            fila=fila,
            usuario_id='PRESENCIAL',
            numero_ticket=numero_senha,
            codigo_qr=codigo_qr,
            prioridade=0,
            e_fisico=True,
            status='Pendente',
            emitido_em=timezone.now(),
            expira_em=timezone.now() + timedelta(hours=4)
        )

        fila.tickets_ativos += 1
        fila.save()
        senha.save()

        tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, numero_senha, 0)
        posicao = max(0, senha.numero_ticket - fila.ticket_atual)
        pdf_buffer = ServicoFila.gerar_pdf_senha(senha, posicao, tempo_espera)
        pdf_base64 = pdf_buffer.getvalue().hex()

        senha.dados_recibo = ServicoFila.gerar_comprovante(senha)
        log_auditoria = LogAuditoria(
            id=uuid.uuid4(),
            id_usuario=None,
            acao='GERAR_SENHA_FISICA_USUARIO',
            tipo_recurso='Senha',
            id_recurso=str(senha.id),
            detalhes=f"Senha {codigo_qr} gerada via mesa digital para fila {fila.servico} (IP: {ip_cliente})",
            data_hora=timezone.now()
        )
        log_auditoria.save()

        try:
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"fila_{fila.id}",
                {
                    "type": "atualizacao_fila",
                    "mensagem": {
                        "fila_id": str(fila.id),
                        "tickets_ativos": fila.tickets_ativos,
                        "ticket_atual": fila.ticket_atual,
                        "mensagem": f"Nova senha emitida: {fila.prefixo}{numero_senha}"
                    }
                }
            )
        except Exception as e:
            logger.error(f"Erro ao enviar atualização de fila via WebSocket: {e}")

        logger.info(f"Senha física {senha.id} gerada via totem para fila {fila_id}")
        return {
            'senha': {
                'id': str(senha.id),
                'fila_id': str(senha.fila_id),
                'numero_senha': senha.numero_ticket,
                'codigo_qr': senha.codigo_qr,
                'status': senha.status,
                'emitido_em': senha.emitido_em.isoformat(),
                'expira_em': senha.expira_em.isoformat()
            },
            'pdf': pdf_base64
        }

    @staticmethod
    @transaction.atomic
    def chamar_proximo(servico, filial_id=None):
        consulta = Fila.objects.filter(servico=servico)
        if filial_id:
            consulta = consulta.filter(departamento__filial__id=filial_id)
        fila = consulta.first()

        if not fila:
            logger.warning(f"Fila {servico} não encontrada")
            raise ValueError("Fila não encontrada")

        if not ServicoFila.esta_fila_aberta(fila):
            logger.warning(f"Fila {fila.id} está fechada para chamar próximo")
            raise ValueError("Fila está fechada no momento")

        if fila.tickets_ativos == 0:
            logger.warning(f"Fila {servico} está vazia")
            raise ValueError("Fila vazia")

        proxima_senha = Ticket.objects.filter(fila_id=fila.id, status='Pendente')\
            .order_by('-prioridade', 'numero_ticket').first()
        if not proxima_senha:
            logger.warning(f"Não há senhas pendentes na fila {fila.id}")
            raise ValueError("Nenhuma senha pendente")

        agora = timezone.now()
        proxima_senha.expira_em = agora + timedelta(minutes=ServicoFila.MINUTOS_TIMEOUT_CHAMADA)
        fila.ticket_atual = proxima_senha.numero_ticket
        fila.tickets_ativos -= 1
        fila.ultimo_balcao = (fila.ultimo_balcao % fila.num_balcoes) + 1
        proxima_senha.status = 'Chamado'
        proxima_senha.balcao = fila.ultimo_balcao
        proxima_senha.atendido_em = agora
        proxima_senha.save()
        fila.save()

        mensagem = f"Dirija-se ao guichê {proxima_senha.balcao:02d}! Senha {fila.prefixo}{proxima_senha.numero_ticket} chamada."
        ServicoFila.enviar_notificacao(None, mensagem, proxima_senha.id, via_websocket=True, usuario_id=proxima_senha.usuario_id)

        try:
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"fila_{fila.id}",
                {
                    "type": "atualizacao_fila",
                    "mensagem": {
                        "fila_id": str(fila.id),
                        "tickets_ativos": fila.tickets_ativos,
                        "ticket_atual": fila.ticket_atual,
                        "mensagem": f"Senha {fila.prefixo}{proxima_senha.numero_ticket} chamada"
                    }
                }
            )
        except Exception as e:
            logger.error(f"Erro ao enviar atualização de fila via WebSocket: {e}")

        logger.info(f"Senha {proxima_senha.id} chamada na fila {servico}")
        return proxima_senha

    @staticmethod
    def verificar_notificacoes_proximidade(usuario_id, lat_usuario, lon_usuario, servico_desejado=None, instituicao_id=None, filial_id=None):
        try:
            perfil = PerfilUsuario.objects.get(usuario_id=usuario_id)
            if not perfil.token_fcm:
                logger.warning(f"Usuário {usuario_id} sem token FCM para notificações de proximidade")
                raise ValueError("Usuário sem token de notificação")
        except ObjectDoesNotExist:
            logger.warning(f"Usuário {usuario_id} não encontrado")
            raise ValueError("Usuário não encontrado")

        if lat_usuario is None or lon_usuario is None:
            logger.warning(f"Localização atual não fornecida para usuario_id={usuario_id}")
            raise ValueError("Localização atual do usuário é obrigatória")

        perfil.ultima_latitude = lat_usuario
        perfil.ultima_longitude = lon_usuario
        perfil.ultima_atualizacao_local = timezone.now()
        perfil.save()
        logger.debug(f"Localização atualizada para usuario_id={usuario_id}: lat={lat_usuario}, lon={lon_usuario}")

        preferencias = PreferenciaUsuario.objects.filter(usuario_id=usuario_id)
        instituicoes_preferidas = {pref.instituicao_id for pref in preferencias if pref.instituicao_id}
        categorias_preferidas = {pref.categoria_id for pref in preferencias if pref.categoria_id}

        consulta = Fila.objects.select_related('departamento__filial__instituicao')
        if instituicao_id:
            consulta = consulta.filter(departamento__filial__instituicao__id=instituicao_id)
        if filial_id:
            consulta = consulta.filter(departamento__filial__id=filial_id)
        if servico_desejado:
            import re
            termos_busca = re.sub(r'[^\w\s]', '', servico_desejado.lower()).split()
            if not termos_busca:
                logger.warning(f"Nenhum termo válido em servico_desejado: {servico_desejado}")
                raise ValueError("Nenhum termo de busca válido fornecido")
            termo_busca = ' & '.join(termos_busca)
            logger.debug(f"Termo de busca full-text: {termo_busca}")
            consulta = consulta.filter(Q(servico__icontains=servico_desejado) | Q(departamento__setor__icontains=servico_desejado))

        filas = consulta.all()
        agora = timezone.now()
        filiais_notificadas = set()

        for fila in filas:
            filial = fila.departamento.filial
            if not filial or not filial.instituicao:
                logger.warning(f"Fila {fila.id} sem filial ou instituição associada")
                continue
            if not ServicoFila.esta_fila_aberta(fila):
                logger.debug(f"Fila {fila.id} fechada, ignorando notificação")
                continue
            if fila.tickets_ativos >= fila.limite_diario:
                logger.debug(f"Fila {fila.id} atingiu limite diário, ignorando notificação")
                continue

            if instituicoes_preferidas and filial.instituicao_id not in instituicoes_preferidas:
                logger.debug(f"Filial {filial.id} não está nas preferências do usuário {usuario_id}")
                continue
            if categorias_preferidas and fila.categoria_id and fila.categoria_id not in categorias_preferidas:
                logger.debug(f"Categoria {fila.categoria_id} não está nas preferências do usuário {usuario_id}")
                continue

            distancia = ServicoFila.calcular_distancia(lat_usuario, lon_usuario, filial)
            if distancia is None or distancia > ServicoFila.LIMITE_PROXIMIDADE_KM:
                logger.debug(f"Filial {filial.id} muito longe: {distancia} km")
                continue

            chave_cache = f'notificacao:{usuario_id}:{filial.id}:{fila.id}:{int(lat_usuario*1000)}:{int(lon_usuario*1000)}'
            if redis_client.get(chave_cache):
                logger.debug(f"Notificação já enviada para usuario_id={usuario_id}, fila_id={fila.id}, localização={lat_usuario},{lon_usuario}")
                continue

            tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, fila.tickets_ativos + 1, 0)
            mensagem = (
                f"Fila disponível próxima! {fila.servico} em {filial.instituicao.nome} ({filial.nome}) "
                f"a {distancia:.2f} km de você. Tempo de espera: {tempo_espera if tempo_espera != 'N/A' else 'Aguardando início'} min."
            )

            ServicoFila.enviar_notificacao(
                perfil.token_fcm,
                mensagem,
                via_websocket=True,
                usuario_id=usuario_id
            )

            redis_client.setex(chave_cache, 3600, 'enviada')
            filiais_notificadas.add(filial.id)
            logger.info(f"Notificação de proximidade enviada para usuario_id={usuario_id}: {mensagem}")

    @staticmethod
    def verificar_notificacoes_proativas():
        agora = timezone.now()
        senhas = Ticket.objects.filter(status='Pendente')
        for senha in senhas:
            fila = senha.fila
            if not ServicoFila.esta_fila_aberta(fila):
                senha.status = 'Cancelado'
                fila.tickets_ativos -= 1
                fila.save()
                senha.save()
                ServicoFila.enviar_notificacao(
                    None,
                    f"Sua senha {fila.prefixo}{senha.numero_ticket} foi cancelada porque o horário de atendimento terminou.",
                    usuario_id=senha.usuario_id
                )
                logger.info(f"Senha {senha.id} cancelada devido ao fim do horário de atendimento")
                continue

            tempo_espera = ServicoFila.calcular_tempo_espera(senha.fila_id, senha.numero_ticket, senha.prioridade)
            if tempo_espera == "N/A":
                continue

            if tempo_espera <= 5 and senha.usuario_id != 'PRESENCIAL':
                distancia = ServicoFila.calcular_distancia(-8.8147, 13.2302, senha.fila.departamento.filial)
                msg_distancia = f" Você está a {distancia} km." if distancia else ""
                mensagem = f"Sua vez está próxima! {senha.fila.servico}, Senha {senha.fila.prefixo}{senha.numero_ticket}. Prepare-se em {tempo_espera} min.{msg_distancia}"
                ServicoFila.enviar_notificacao(None, mensagem, senha.id, via_websocket=True, usuario_id=senha.usuario_id)

            if senha.usuario_id != 'PRESENCIAL':
                try:
                    perfil = PerfilUsuario.objects.get(usuario_id=senha.usuario_id)
                    if perfil.ultima_latitude and perfil.ultima_longitude and perfil.ultima_atualizacao_local:
                        if (timezone.now() - perfil.ultima_atualizacao_local).total_seconds() < 600:
                            distancia = ServicoFila.calcular_distancia(perfil.ultima_latitude, perfil.ultima_longitude, senha.fila.departamento.filial)
                            if distancia and distancia > 5:
                                tempo_viagem = distancia * 2
                                if tempo_espera <= tempo_viagem:
                                    mensagem = f"Você está a {distancia} km! Senha {senha.fila.prefixo}{senha.numero_ticket} será chamada em {tempo_espera} min. Comece a se deslocar!"
                                    ServicoFila.enviar_notificacao(None, mensagem, senha.id, via_websocket=True, usuario_id=senha.usuario_id)
                        else:
                            logger.debug(f"Localização do usuário {senha.usuario_id} desatualizada: {perfil.ultima_atualizacao_local}")
                except ObjectDoesNotExist:
                    logger.debug(f"Perfil não encontrado para usuario_id={senha.usuario_id}")

        senhas_chamadas = Ticket.objects.filter(status='Chamado')
        for senha in senhas_chamadas:
            if senha.expira_em and senha.expira_em < agora:
                senha.status = 'Cancelado'
                senha.fila.tickets_ativos -= 1
                senha.fila.save()
                senha.save()
                ServicoFila.enviar_notificacao(
                    None,
                    f"Sua senha {senha.fila.prefixo}{senha.numero_ticket} foi cancelada porque você não validou a presença a tempo.",
                    usuario_id=senha.usuario_id
                )
                logger.info(f"Senha {senha.id} cancelada por falta de validação de presença")

    @staticmethod
    @transaction.atomic
    def trocar_senhas(senha_de_id, senha_para_id, usuario_de_id):
        try:
            senha_de = Ticket.objects.get(id=senha_de_id)
            senha_para = Ticket.objects.get(id=senha_para_id)
        except ObjectDoesNotExist:
            logger.warning(f"Uma das senhas não encontrada: {senha_de_id}, {senha_para_id}")
            raise ValueError("Senha não encontrada")

        if senha_de.usuario_id != usuario_de_id or not senha_para.troca_disponivel or \
           senha_de.fila_id != senha_para.fila_id or senha_de.status != 'Pendente' or \
           senha_para.status != 'Pendente':
            logger.warning(f"Tentativa inválida de troca entre {senha_de_id} e {senha_para_id}")
            raise ValueError("Troca inválida")

        usuario_de, usuario_para = senha_de.usuario_id, senha_para.usuario_id
        num_de, num_para = senha_de.numero_ticket, senha_para.numero_ticket
        senha_de.usuario_id, senha_de.numero_ticket = usuario_para, num_para
        senha_para.usuario_id, senha_para.numero_ticket = usuario_de, num_de
        senha_de.troca_disponivel, senha_para.troca_disponivel = False, False
        senha_de.save()
        senha_para.save()

        logger.info(f"Troca realizada entre {senha_de_id} e {senha_para_id}")

        ServicoFila.enviar_notificacao(
            None,
            f"Sua senha foi trocada! Nova senha: {senha_de.fila.prefixo}{senha_de.numero_ticket}",
            senha_de.id,
            via_websocket=True,
            usuario_id=senha_de.usuario_id
        )
        ServicoFila.enviar_notificacao(
            None,
            f"Sua senha foi trocada! Nova senha: {senha_para.fila.prefixo}{senha_para.numero_ticket}",
            senha_para.id,
            via_websocket=True,
            usuario_id=senha_para.usuario_id
        )

        return {"senha_de": senha_de, "senha_para": senha_para}

    @staticmethod
    @transaction.atomic
    def validar_presenca(codigo_qr, lat_usuario=None, lon_usuario=None):
        try:
            senha = Ticket.objects.get(codigo_qr=codigo_qr)
        except ObjectDoesNotExist:
            logger.warning(f"Tentativa inválida de validar presença com QR {codigo_qr}")
            raise ValueError("Senha inválida ou não chamada")

        if senha.status != 'Chamado':
            logger.warning(f"Senha {senha.id} no estado {senha.status} não pode ser validada")
            raise ValueError("Senha não chamada")

        if not ServicoFila.esta_fila_aberta(senha.fila):
            logger.warning(f"Fila {senha.fila.id} está fechada para validação de presença")
            raise ValueError("Fila está fechada no momento")

        if lat_usuario and lon_usuario:
            filial = senha.fila.departamento.filial
            distancia = ServicoFila.calcular_distancia(lat_usuario, lon_usuario, filial)
            if distancia and distancia > ServicoFila.LIMITE_PROXIMIDADE_PRESENCA_KM:
                logger.warning(f"Usuário muito longe para validar presença: {distancia} km")
                raise ValueError(f"Você está muito longe da filial ({distancia:.2f} km). Aproxime-se para validar.")

        senha.status = 'Atendido'
        senha.atendido_em = timezone.now()

        fila = senha.fila
        ultima_senha = Ticket.objects.filter(fila_id=fila.id, status='Atendido', atendido_em__lt=senha.atendido_em)\
            .order_by('-atendido_em').first()
        if ultima_senha and ultima_senha.atendido_em:
            senha.tempo_servico = (senha.atendido_em - ultima_senha.atendido_em).total_seconds() / 60.0
            fila.ultimo_tempo_servico = senha.tempo_servico

        fila.save()
        senha.save()
        logger.info(f"Presença validada para senha {senha.id}")
        return senha

    @staticmethod
    @transaction.atomic
    def oferecer_troca(senha_id, usuario_id):
        logger.info(f"Iniciando oferta de troca para senha {senha_id} por usuario_id {usuario_id}")
        try:
            senha = Ticket.objects.get(id=senha_id)
        except ObjectDoesNotExist:
            logger.warning(f"Senha {senha_id} não encontrada")
            raise ValueError("Senha não encontrada")

        if senha.usuario_id != usuario_id:
            logger.warning(f"Tentativa inválida de oferecer senha {senha_id} por {usuario_id}")
            raise ValueError("Você só pode oferecer sua própria senha.")
        if senha.status != 'Pendente':
            logger.warning(f"Senha {senha_id} no estado {senha.status} não pode ser oferecida")
            raise ValueError(f"Esta senha está no estado '{senha.status}' e não pode ser oferecida.")
        if senha.troca_disponivel:
            logger.warning(f"Senha {senha_id} já está oferecida para troca")
            raise ValueError("Esta senha já está oferecida para troca.")

        senha.troca_disponivel = True
        senha.save()
        logger.info(f"Senha {senha_id} oferecida para troca com sucesso")

        ServicoFila.enviar_notificacao(
            None,
            f"Sua senha {senha.fila.prefixo}{senha.numero_ticket} foi oferecida para troca!",
            senha.id,
            via_websocket=True,
            usuario_id=usuario_id
        )

        senhas_elegiveis = Ticket.objects.filter(
            fila_id=senha.fila_id,
            usuario_id__ne=usuario_id,
            status='Pendente',
            troca_disponivel=False
        ).order_by('emitido_em')[:5]

        try:
            camada_canal = get_channel_layer()
            for senha_elegivel in senhas_elegiveis:
                async_to_sync(camada_canal.group_send)(
                    f"usuario_{senha_elegivel.usuario_id}",
                    {
                        "type": "troca_disponivel",
                        "mensagem": {
                            "senha_id": str(senha.id),
                            "fila_id": str(senha.fila_id),
                            "servico": senha.fila.servico,
                            "numero": f"{senha.fila.prefixo}{senha.numero_ticket}",
                            "posicao": max(0, senha.numero_ticket - senha.fila.ticket_atual)
                        }
                    }
                )
                logger.debug(f"Evento troca_disponivel emitido para usuario_id {senha_elegivel.usuario_id}")
        except Exception as e:
            logger.error(f"Erro ao emitir troca_disponivel: {e}")

        return senha

    @staticmethod
    @transaction.atomic
    def cancelar_senha(senha_id, usuario_id):
        try:
            senha = Ticket.objects.get(id=senha_id)
        except ObjectDoesNotExist:
            logger.warning(f"Senha {senha_id} não encontrada")
            raise ValueError("Senha não encontrada")

        if senha.usuario_id != usuario_id:
            logger.warning(f"Tentativa inválida de cancelar senha {senha_id} por usuario_id={usuario_id}")
            raise ValueError("Você só pode cancelar sua própria senha")
        if senha.status != 'Pendente':
            logger.warning(f"Senha {senha_id} no estado {senha.status} não pode ser cancelada")
            raise ValueError("Esta senha não pode ser cancelada no momento")

        senha.status = 'Cancelado'
        senha.fila.tickets_ativos -= 1
        senha.fila.save()
        senha.save()

        ServicoFila.enviar_notificacao(
            None,
            f"Sua senha {senha.fila.prefixo}{senha.numero_ticket} foi cancelada.",
            senha.id,
            via_websocket=True,
            usuario_id=usuario_id
        )

        try:
            camada_canal = get_channel_layer()
            async_to_sync(camada_canal.group_send)(
                f"fila_{senha.fila_id}",
                {
                    "type": "atualizacao_fila",
                    "mensagem": {
                        "fila_id": str(senha.fila_id),
                        "tickets_ativos": senha.fila.tickets_ativos,
                        "ticket_atual": senha.fila.ticket_atual,
                        "mensagem": f"Senha {senha.fila.prefixo}{senha.numero_ticket} cancelada"
                    }
                }
            )
        except Exception as e:
            logger.error(f"Erro ao enviar atualização de fila via WebSocket: {e}")

        logger.info(f"Senha {senha.id} cancelada por usuario_id={usuario_id}")
        return senha

    @staticmethod
    def esta_fila_aberta(fila, agora=None):
        if not agora:
            agora = timezone.now()

        dia_semana = agora.strftime('%A').capitalize()
        try:
            dia_enum = {
                'Monday': 'Segunda',
                'Tuesday': 'Terça',
                'Wednesday': 'Quarta',
                'Thursday': 'Quinta',
                'Friday': 'Sexta',
                'Saturday': 'Sábado',
                'Sunday': 'Domingo'
            }[dia_semana]
            horario = HorarioFila.objects.filter(fila_id=fila.id, dia_semana=dia_enum).first()

            if not horario or horario.esta_fechado:
                return False

            hora_atual = agora.time()
            return horario.hora_abertura <= hora_atual <= horario.hora_fechamento
        except KeyError:
            logger.error(f"Dia da semana inválido: {dia_semana}")
            return False

    @staticmethod
    def buscar_servicos(
        termo_busca,
        usuario_id=None,
        lat_usuario=None,
        lon_usuario=None,
        nome_instituicao=None,
        bairro=None,
        filial_id=None,
        max_resultados=5,
        max_distancia_km=10.0,
        pagina=1,
        por_pagina=20
    ):
        agora = timezone.now()
        resultados = []

        consulta_base = Fila.objects.select_related('departamento__filial__instituicao').prefetch_related('horarios')

        if termo_busca:
            import re
            termos_busca = re.sub(r'[^\w\s]', '', termo_busca.lower()).split()
            if not termos_busca:
                logger.warning(f"Nenhum termo válido em termo_busca: {termo_busca}")
                raise ValueError("Nenhum termo de busca válido fornecido")
            termo_busca = ' & '.join(termos_busca)
            logger.debug(f"Termo de busca: {termo_busca}")
            consulta_base = consulta_base.filter(
                Q(servico__icontains=termo_busca) |
                Q(departamento__setor__icontains=termo_busca) |
                Q(departamento__filial__instituicao__nome__icontains=termo_busca)
            ).filter(
                id__in=EtiquetaServico.objects.filter(
                    etiqueta__ilike=f'%{termo_busca}%'
                ).values('fila_id')
            )

        if nome_instituicao:
            consulta_base = consulta_base.filter(departamento__filial__instituicao__nome__ilike=f'%{nome_instituicao}%')

        if bairro:
            consulta_base = consulta_base.filter(departamento__filial__bairro__ilike=f'%{bairro}%')

        if filial_id:
            consulta_base = consulta_base.filter(departamento__filial__id=filial_id)

        consulta_base = consulta_base.filter(
            horarios__dia_semana=agora.strftime('%A').capitalize(),
            horarios__esta_fechado=False,
            horarios__hora_abertura__lte=agora.time(),
            horarios__hora_fechamento__gte=agora.time(),
            tickets_ativos__lt=F('limite_diario')
        )

        preferencias_usuario = None
        if usuario_id:
            preferencias_usuario = PreferenciaUsuario.objects.filter(usuario_id=usuario_id)

        total = consulta_base.count()
        filas = consulta_base.order_by('servico')[(pagina - 1) * por_pagina:pagina * por_pagina]

        for fila in filas:
            filial = fila.departamento.filial
            instituicao = filial.instituicao

            distancia = None
            if lat_usuario and lon_usuario and filial.latitude and filial.longitude:
                distancia = ServicoFila.calcular_distancia(lat_usuario, lon_usuario, filial)
                if distancia and distancia > max_distancia_km:
                    continue

            tempo_espera = ServicoFila.calcular_tempo_espera(fila.id, fila.tickets_ativos + 1, 0)

            rotulo_velocidade = "Desconhecida"
            senhas = Ticket.objects.filter(fila_id=fila.id, status='Atendido')
            tempos_servico = [s.tempo_servico for s in senhas if s.tempo_servico is not None and s.tempo_servico > 0]
            if tempos_servico:
                tempo_medio_servico = np.mean(tempos_servico)
                if tempo_medio_servico <= 5:
                    rotulo_velocidade = "Rápida"
                elif tempo_medio_servico <= 15:
                    rotulo_velocidade = "Moderada"
                else:
                    rotulo_velocidade = "Lenta"

            pontuacao = 0.0
            pontuacao_qualidade = preditor_recomendacao_servico.prever(fila)
            if termo_busca and termos_busca:
                pontuacao += 0.4  # Simplificação, substituir por ranking real se necessário
            if distancia:
                pontuacao += (1 / (distancia + 1)) * 0.3
            pontuacao += pontuacao_qualidade * 0.2
            if preferencias_usuario:
                if any(pref.instituicao_id == instituicao.id for pref in preferencias_usuario):
                    pontuacao += 0.2
                if any(pref.categoria_id == fila.categoria_id for pref in preferencias_usuario):
                    pontuacao += 0.2

            resultados.append({
                'instituicao': {
                    'id': str(instituicao.id),
                    'nome': instituicao.nome
                },
                'filial': {
                    'id': str(filial.id),
                    'nome': filial.nome,
                    'localizacao': filial.localizacao,
                    'bairro': filial.bairro,
                    'latitude': filial.latitude,
                    'longitude': filial.longitude
                },
                'fila': {
                    'id': str(fila.id),
                    'servico': fila.servico,
                    'categoria_id': str(fila.categoria_id) if fila.categoria_id else None,
                    'tempo_espera': tempo_espera if tempo_espera != 'N/A' else 'Aguardando início',
                    'distancia': distancia if distancia is not None else 'Desconhecida',
                    'tickets_ativos': fila.tickets_ativos,
                    'limite_diario': fila.limite_diario,
                    'hora_abertura': fila.hora_abertura.strftime('%H:%M') if fila.hora_abertura else None,
                    'hora_fechamento': fila.hora_fechamento.strftime('%H:%M') if fila.hora_fechamento else None,
                    'pontuacao_qualidade': pontuacao_qualidade,
                    'rotulo_velocidade': rotulo_velocidade
                },
                'pontuacao': pontuacao
            })

        resultados.sort(key=lambda x: x['pontuacao'], reverse=True)

        sugestoes = []
        if resultados and resultados[0]['fila']['categoria_id']:
            filas_relacionadas = Fila.objects.filter(
                categoria_id=resultados[0]['fila']['categoria_id'],
                id__ne=resultados[0]['fila']['id']
            ).select_related('departamento__filial__instituicao')[:3]
            for fila in filas_relacionadas:
                sugestoes.append({
                    'fila_id': str(fila.id),
                    'instituicao': fila.departamento.filial.instituicao.nome,
                    'filial': fila.departamento.filial.nome,
                    'servico': fila.servico,
                    'tempo_espera': ServicoFila.calcular_tempo_espera(fila.id, fila.tickets_ativos + 1, 0)
                })

        resultado = {
            'servicos': resultados[:max_resultados],
            'total': total,
            'pagina': pagina,
            'por_pagina': por_pagina,
            'sugestoes': sugestoes
        }

        try:
            chave_cache = f'servicos:{termo_busca}:{nome_instituicao}:{bairro}:{filial_id}'
            redis_client.setex(chave_cache, 30, json.dumps(resultado))
        except Exception as e:
            logger.warning(f"Erro ao salvar cache no Redis para {chave_cache}: {e}")

        return resultado

    @staticmethod
    def obter_dados_painel(instituicao_id):
        filiais = Filial.objects.filter(instituicao_id=instituicao_id)
        resultado = {'filiais': []}

        for filial in filiais:
            filas = Fila.objects.filter(departamento__filial=filial)
            dados_filial = {
                'filial_id': str(filial.id),
                'nome_filial': filial.nome,
                'bairro': filial.bairro,
                'filas': []
            }

            for fila in filas:
                senha_atual = Ticket.objects.filter(fila_id=fila.id, status='Chamado').order_by('-atendido_em').first()
                chamada_atual = None
                if senha_atual:
                    chamada_atual = {
                        'numero_senha': f"{fila.prefixo}{senha_atual.numero_ticket}",
                        'balcao': senha_atual.balcao or fila.ultimo_balcao or 1,
                        'data_hora': senha_atual.atendido_em.isoformat() if senha_atual.atendido_em else None
                    }

                senhas_recentes = Ticket.objects.filter(fila_id=fila.id, status='Atendido')\
                    .order_by('-atendido_em')[:5]
                dados_chamadas_recentes = [
                    {
                        'numero_senha': f"{fila.prefixo}{senha.numero_ticket}",
                        'balcao': senha.balcao or fila.ultimo_balcao or 1,
                        'data_hora': senha.atendido_em.isoformat()
                    } for senha in senhas_recentes
                ]

                dados_filial['filas'].append({
                    'fila_id': str(fila.id),
                    'nome': fila.servico,
                    'servico': fila.servico or 'Atendimento Geral',
                    'chamada_atual': chamada_atual,
                    'chamadas_recentes': dados_chamadas_recentes,
                    'categoria_id': str(fila.categoria_id) if fila.categoria_id else None
                })

            resultado['filiais'].append(dados_filial)

        try:
            chave_cache = f'painel:{instituicao_id}'
            redis_client.setex(chave_cache, 10, json.dumps(resultado))
        except Exception as e:
            logger.warning(f"Erro ao salvar cache no Redis para {chave_cache}: {e}")

        return resultado

    @staticmethod
    def emitir_atualizacao_painel(instituicao_id, fila_id, tipo_evento, dados):
        canal = f'painel:{instituicao_id}'
        mensagem = {
            'evento': tipo_evento,
            'fila_id': str(fila_id),
            'dados': dados,
            'data_hora': timezone.now().isoformat()
        }
        try:
            redis_client.publish(canal, json.dumps(mensagem))
        except Exception as e:
            logger.warning(f"Erro ao publicar atualização de painel para {canal}: {e}")

    @staticmethod
    def inscrever_no_painel(instituicao_id):
        pubsub = redis_client.pubsub()
        try:
            pubsub.subscribe(f'painel:{instituicao_id}')
        except Exception as e:
            logger.error(f"Erro ao subscrever painel {instituicao_id}: {e}")
        return pubsub