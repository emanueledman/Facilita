from celery import shared_task
from fila_online.models import Fila
from fila_online.preditores_ml import preditor_tempo_espera, preditor_recomendacao_servico
import logging

logger = logging.getLogger(__name__)

@shared_task
def treinar_modelos_periodicamente():
    logger.info("Iniciando treinamento periódico dos modelos de ML.")
    try:
        filas = Fila.objects.all()
        for fila in filas:
            logger.info(f"Treinando PreditorTempoEspera para fila_id={fila.id}")
            preditor_tempo_espera.treinar(str(fila.id))
        logger.info("Treinando PreditorRecomendacaoServico")
        preditor_recomendacao_servico.treinar()
        logger.info("Treinamento periódico concluído com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao treinar modelos de ML: {str(e)}")