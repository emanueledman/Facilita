import logging
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
import joblib
import os
from datetime import datetime, timedelta
from django.utils import timezone
from fila_online.models import Fila, Ticket, Departamento
from django.conf import settings

logger = logging.getLogger(__name__)
logger.debug("Iniciando carregamento do módulo preditores_ml")

# Instanciar os preditores globalmente dentro de um try-except
try:
    logger.debug("Instanciando preditor_tempo_espera e preditor_recomendacao_servico")
    preditor_tempo_espera = None
    preditor_recomendacao_servico = None
except Exception as e:
    logger.error(f"Erro ao inicializar o módulo preditores_ml: {e}")
    raise

# Definição das classes fora do try-except
class PreditorTempoEspera:
    CAMINHO_MODELO = os.path.join(settings.BASE_DIR, "preditor_tempo_espera.joblib")
    CAMINHO_SCALER = os.path.join(settings.BASE_DIR, "scaler_tempo_espera.joblib")
    AMOSTRAS_MINIMAS = 10
    DIAS_MAXIMOS = 30

    def __init__(self):
        self.modelo = RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)
        self.scaler = StandardScaler()
        self.esta_treinado = {}
        self.tempos_fallback = {}  # Cache de tempos médios por fila
        self.carregar_modelo()

    def carregar_modelo(self):
        """Carrega o modelo e o scaler salvos, se existirem."""
        try:
            if os.path.exists(self.CAMINHO_MODELO) and os.path.exists(self.CAMINHO_SCALER):
                self.modelo = joblib.load(self.CAMINHO_MODELO)
                self.scaler = joblib.load(self.CAMINHO_SCALER)
                self.esta_treinado = {str(fila.id): True for fila in Fila.objects.all()}
                logger.info("Modelo de previsão de tempo de espera carregado com sucesso.")
            else:
                logger.info("Modelo de tempo de espera não encontrado. Será treinado por fila na primeira execução.")
            # Inicializar fallbacks
            self._calcular_tempos_fallback()
        except Exception as e:
            logger.error(f"Erro ao carregar o modelo de tempo de espera: {e}")
            self.esta_treinado = {}

    def salvar_modelo(self):
        """Salva o modelo e o scaler em disco."""
        try:
            joblib.dump(self.modelo, self.CAMINHO_MODELO)
            joblib.dump(self.scaler, self.CAMINHO_SCALER)
            logger.info("Modelo de previsão de tempo de espera salvo com sucesso.")
        except Exception as e:
            logger.error(f"Erro ao salvar o modelo de tempo de espera: {e}")

    def _calcular_tempos_fallback(self):
        """Calcula tempos médios de espera por fila para uso como fallback."""
        try:
            filas = Fila.objects.all()
            for fila in filas:
                tickets = Ticket.objects.filter(
                    fila_id=fila.id,
                    status='Atendido',
                    tempo_servico__isnull=False,
                    tempo_servico__gt=0
                )[:100]
                if tickets:
                    tempo_medio = np.mean([t.tempo_servico for t in tickets])
                    self.tempos_fallback[str(fila.id)] = round(tempo_medio, 1)
                else:
                    self.tempos_fallback[str(fila.id)] = fila.tempo_espera_medio or 30
            logger.debug(f"Tempos fallback calculados para {len(self.tempos_fallback)} filas")
        except Exception as e:
            logger.error(f"Erro ao calcular tempos fallback: {e}")

    def preparar_dados(self, fila_id, dias=DIAS_MAXIMOS):
        """Prepara os dados históricos para treinamento por fila."""
        try:
            fila = Fila.objects.get(id=fila_id)
        except Fila.DoesNotExist:
            logger.error(f"Fila não encontrada: fila_id={fila_id}")
            return None, None

        data_inicio = timezone.now() - timedelta(days=dias)
        tickets = Ticket.objects.filter(
            fila_id=fila_id,
            status='Atendido',
            emitido_em__gte=data_inicio,
            tempo_servico__isnull=False,
            tempo_servico__gt=0
        )

        if tickets.count() < self.AMOSTRAS_MINIMAS:
            logger.warning(f"Dados insuficientes para fila_id={fila_id}: {tickets.count()} amostras")
            return None, None

        dados = []
        for ticket in tickets:
            posicao = max(0, ticket.numero_ticket - fila.ticket_atual)
            hora_do_dia = ticket.emitido_em.hour
            setor_codificado = hash(fila.departamento.setor) % 100 if fila.departamento.setor else 0
            dados.append({
                'posicao': posicao,
                'tickets_ativos': fila.tickets_ativos,
                'prioridade': ticket.prioridade or 0,
                'hora_do_dia': hora_do_dia,
                'num_balcoes': fila.num_balcoes or 1,
                'limite_diario': fila.limite_diario or 100,
                'setor_codificado': setor_codificado,
                'tempo_servico': ticket.tempo_servico
            })

        df = pd.DataFrame(dados)
        X = df[['posicao', 'tickets_ativos', 'prioridade', 'hora_do_dia', 'num_balcoes', 'limite_diario', 'setor_codificado']]
        y = df['tempo_servico']
        logger.debug(f"Dados preparados para fila_id={fila_id}: {len(dados)} amostras")
        return X, y

    def treinar(self, fila_id):
        """Treina o modelo com dados históricos de uma fila específica."""
        try:
            X, y = self.preparar_dados(fila_id)
            if X is None or y is None:
                self.esta_treinado[fila_id] = False
                return False

            X_treino, X_teste, y_treino, y_teste = train_test_split(X, y, test_size=0.2, random_state=42)
            X_treino_escalado = self.scaler.fit_transform(X_treino)
            X_teste_escalado = self.scaler.transform(X_teste)
            self.modelo.fit(X_treino_escalado, y_treino)
            self.esta_treinado[fila_id] = True
            pontuacao = self.modelo.score(X_teste_escalado, y_teste)
            logger.info(f"Modelo treinado para fila_id={fila_id}. Pontuação R²: {pontuacao:.2f}")
            self.salvar_modelo()
            self._calcular_tempos_fallback()  # Atualizar fallbacks após treinamento
            return True
        except Exception as e:
            logger.error(f"Erro ao treinar modelo para fila_id={fila_id}: {e}")
            self.esta_treinado[fila_id] = False
            return False

    def prever(self, fila_id, posicao, tickets_ativos, prioridade, hora_do_dia):
        """Faz uma previsão do tempo de espera para uma fila."""
        try:
            if not isinstance(fila_id, str) or not fila_id:
                logger.error(f"fila_id inválido: {fila_id}")
                return self.tempos_fallback.get(fila_id, 30)

            try:
                fila = Fila.objects.get(id=fila_id)
            except Fila.DoesNotExist:
                logger.error(f"Fila não encontrada: fila_id={fila_id}")
                return self.tempos_fallback.get(fila_id, 30)

            if fila_id not in self.esta_treinado or not self.esta_treinado[fila_id]:
                logger.warning(f"Modelo não treinado para fila_id={fila_id}. Usando fallback.")
                return self.tempos_fallback.get(fila_id, fila.tempo_espera_medio or 30)

            setor_codificado = hash(fila.departamento.setor) % 100 if fila.departamento.setor else 0
            caracteristicas = np.array([[
                max(0, posicao),
                max(0, tickets_ativos),
                prioridade or 0,
                max(0, min(23, hora_do_dia)),
                fila.num_balcoes or 1,
                fila.limite_diario or 100,
                setor_codificado
            ]])
            caracteristicas_escaladas = self.scaler.transform(caracteristicas)
            tempo_previsto = self.modelo.predict(caracteristicas_escaladas)[0]
            tempo_previsto = max(0, tempo_previsto)
            logger.debug(f"Previsão de tempo de espera para fila_id={fila_id}: {tempo_previsto:.1f} minutos")
            return round(tempo_previsto, 1)
        except Exception as e:
            logger.error(f"Erro ao prever tempo de espera para fila_id={fila_id}: {e}")
            return self.tempos_fallback.get(fila_id, fila.tempo_espera_medio or 30)

class PreditorRecomendacaoServico:
    CAMINHO_MODELO = os.path.join(settings.BASE_DIR, "preditor_recomendacao_servico.joblib")
    CAMINHO_SCALER = os.path.join(settings.BASE_DIR, "scaler_recomendacao_servico.joblib")
    AMOSTRAS_MINIMAS = 5
    PONTUACAO_PADRAO = 0.5

    def __init__(self):
        self.modelo = RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)
        self.scaler = StandardScaler()
        self.esta_treinado = False
        self.pontuacoes_fallback = {}  # Cache de pontuações médias por fila
        self.carregar_modelo()

    def carregar_modelo(self):
        """Carrega o modelo e o scaler salvos, se existirem."""
        try:
            if os.path.exists(self.CAMINHO_MODELO) and os.path.exists(self.CAMINHO_SCALER):
                self.modelo = joblib.load(self.CAMINHO_MODELO)
                self.scaler = joblib.load(self.CAMINHO_SCALER)
                self.esta_treinado = True
                logger.info("Modelo de recomendação de serviços carregado com sucesso.")
            else:
                logger.info("Modelo de recomendação não encontrado. Será treinado na primeira execução.")
            self._calcular_pontuacoes_fallback()
        except Exception as e:
            logger.error(f"Erro ao carregar o modelo de recomendação: {e}")
            self.esta_treinado = False

    def salvar_modelo(self):
        """Salva o modelo e o scaler em disco."""
        try:
            joblib.dump(self.modelo, self.CAMINHO_MODELO)
            joblib.dump(self.scaler, self.CAMINHO_SCALER)
            logger.info("Modelo de recomendação de serviços salvo com sucesso.")
        except Exception as e:
            logger.error(f"Erro ao salvar o modelo de recomendação: {e}")

    def _calcular_pontuacoes_fallback(self):
        """Calcula pontuações médias de qualidade por fila para uso como fallback."""
        try:
            filas = Fila.objects.all()
            for fila in filas:
                tickets = Ticket.objects.filter(
                    fila_id=fila.id,
                    status='Atendido',
                    tempo_servico__isnull=False,
                    tempo_servico__gt=0
                )[:100]
                if tickets:
                    tempos_servico = [t.tempo_servico for t in tickets]
                    tempo_medio = np.mean(tempos_servico)
                    disponibilidade = max(0, fila.limite_diario - fila.tickets_ativos) / max(1, fila.limite_diario)
                    pontuacao = (1 / (1 + tempo_medio / 60)) * disponibilidade
                    self.pontuacoes_fallback[str(fila.id)] = max(0, min(1, round(pontuacao, 2)))
                else:
                    self.pontuacoes_fallback[str(fila.id)] = self.PONTUACAO_PADRAO
            logger.debug(f"Pontuações fallback calculadas para {len(self.pontuacoes_fallback)} filas")
        except Exception as e:
            logger.error(f"Erro ao calcular pontuações fallback: {e}")

    def preparar_dados(self):
        """Prepara os dados históricos para treinamento do modelo de recomendação."""
        try:
            filas = Fila.objects.all()
            if not filas:
                logger.warning("Nenhuma fila disponível para treinamento do modelo de recomendação.")
                return None, None

            dados = []
            for fila in filas:
                tickets = Ticket.objects.filter(
                    fila_id=fila.id,
                    status='Atendido',
                    tempo_servico__isnull=False,
                    tempo_servico__gt=0
                )
                if not tickets:
                    continue

                tempos_servico = [t.tempo_servico for t in tickets]
                tempo_medio_servico = np.mean(tempos_servico)
                desvio_padrao_servico = np.std(tempos_servico) if len(tempos_servico) > 1 else 0
                tempo_servico_por_balcao = tempo_medio_servico / max(1, fila.num_balcoes or 1)
                taxa_ocupacao = fila.tickets_ativos / max(1, fila.limite_diario or 100)
                disponibilidade = max(0, fila.limite_diario - fila.tickets_ativos)
                setor_codificado = hash(fila.departamento.setor) % 100 if fila.departamento.setor else 0
                pontuacao_qualidade = (disponibilidade / max(1, fila.limite_diario)) * (1 / (1 + tempo_medio_servico / 60))
                pontuacao_qualidade = max(0, min(1, pontuacao_qualidade))

                dados.append({
                    'tempo_medio_servico': tempo_medio_servico,
                    'desvio_padrao_servico': desvio_padrao_servico,
                    'tempo_servico_por_balcao': tempo_servico_por_balcao,
                    'taxa_ocupacao': taxa_ocupacao,
                    'disponibilidade': disponibilidade,
                    'setor_codificado': setor_codificado,
                    'hora_do_dia': timezone.now().hour,
                    'dia_da_semana': timezone.now().weekday(),
                    'pontuacao_qualidade': pontuacao_qualidade
                })

            if len(dados) < self.AMOSTRAS_MINIMAS:
                logger.warning(f"Dados insuficientes para treinamento do modelo de recomendação: {len(dados)} amostras")
                return None, None

            df = pd.DataFrame(dados)
            X = df[['tempo_medio_servico', 'desvio_padrao_servico', 'tempo_servico_por_balcao', 'taxa_ocupacao', 'disponibilidade', 'setor_codificado', 'hora_do_dia', 'dia_da_semana']]
            y = df['pontuacao_qualidade']
            logger.debug(f"Dados preparados para modelo de recomendação: {len(dados)} amostras")
            return X, y
        except Exception as e:
            logger.error(f"Erro ao preparar dados: {e}")
            self.esta_treinado = False

    def treinar(self):
        """Treina o modelo com dados históricos."""
        try:
            X, y = self.preparar_dados()
            if X is None or y is None:
                self.esta_treinado = False
                return

            X_treino, X_teste, y_treino, y_teste = train_test_split(X, y, test_size=0.2, random_state=42)
            X_treino_escalado = self.scaler.fit_transform(X_treino)
            X_teste_escalado = self.scaler.transform(X_teste)
            self.modelo.fit(X_treino_escalado, y_treino)
            self.esta_treinado = True
            pontuacao = self.modelo.score(X_teste_escalado, y_teste)
            logger.info(f"Modelo de recomendação treinado com sucesso. Pontuação R²: {pontuacao:.2f}")
            self.salvar_modelo()
            self._calcular_pontuacoes_fallback()  # Atualizar fallbacks após treinamento
        except Exception as e:
            logger.error(f"Erro ao treinar o modelo de recomendação: {e}")
            self.esta_treinado = False

    def prever(self, fila):
        """Faz uma previsão da pontuação de qualidade de atendimento para uma fila."""
        try:
            if not fila or not hasattr(fila, 'id'):
                logger.error("Objeto fila inválido")
                return self.pontuacoes_fallback.get(str(fila.id), self.PONTUACAO_PADRAO) if fila else self.PONTUACAO_PADRAO

            if not self.esta_treinado:
                logger.warning("Modelo de recomendação não treinado. Usando fallback.")
                return self.pontuacoes_fallback.get(str(fila.id), self.PONTUACAO_PADRAO)

            tickets = Ticket.objects.filter(
                fila_id=fila.id,
                status='Atendido',
                tempo_servico__isnull=False,
                tempo_servico__gt=0
            )
            tempos_servico = [t.tempo_servico for t in tickets]
            tempo_medio_servico = np.mean(tempos_servico) if tempos_servico else 30
            desvio_padrao_servico = np.std(tempos_servico) if len(tempos_servico) > 1 else 0
            tempo_servico_por_balcao = tempo_medio_servico / max(1, fila.num_balcoes or 1)
            taxa_ocupacao = fila.tickets_ativos / max(1, fila.limite_diario or 100)
            disponibilidade = max(0, fila.limite_diario - fila.tickets_ativos)
            setor_codificado = hash(fila.departamento.setor) % 100 if fila.departamento.setor else 0

            caracteristicas = np.array([[
                tempo_medio_servico,
                desvio_padrao_servico,
                tempo_servico_por_balcao,
                taxa_ocupacao,
                disponibilidade,
                setor_codificado,
                timezone.now().hour,
                timezone.now().weekday()
            ]])
            caracteristicas_escaladas = self.scaler.transform(caracteristicas)
            pontuacao_qualidade = self.modelo.predict(caracteristicas_escaladas)[0]
            pontuacao_qualidade = max(0, min(1, pontuacao_qualidade))
            logger.debug(f"Previsão de qualidade para fila_id={fila.id}: {pontuacao_qualidade:.2f}")
            return pontuacao_qualidade
        except Exception as e:
            logger.error(f"Erro ao prever qualidade para fila_id={fila.id}: {e}")
            return self.pontuacoes_fallback.get(str(fila.id), self.PONTUACAO_PADRAO)

# Instanciar os preditores após a definição das classes
try:
    preditor_tempo_espera = PreditorTempoEspera()
    preditor_recomendacao_servico = PreditorRecomendacaoServico()
    logger.debug("Instâncias criadas com sucesso")
except Exception as e:
    logger.error(f"Erro ao instanciar preditores: {e}")
    raise

logger.debug("Módulo preditores_ml carregado com sucesso")