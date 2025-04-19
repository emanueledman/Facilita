from django.core.management.base import BaseCommand
from fila_online.models import Fila
from fila_online.ml_models import preditor_tempo_espera, preditor_recomendacao_servico
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Treina os modelos de machine learning periodicamente'

    def handle(self, *args, **kwargs):
        try:
            logger.info("Iniciando treinamento periódico dos modelos de ML.")
            filas = Fila.objects.all()
            for fila in filas:
                logger.info(f"Treinando PreditorTempoEspera para fila_id={fila.id}")
                preditor_tempo_espera.treinar(fila.id)
            logger.info("Treinando PreditorRecomendacaoServico")
            preditor_recomendacao_servico.treinar()
            logger.info("Treinamento periódico concluído.")
            self.stdout.write(self.style.SUCCESS('Modelos ML treinados com sucesso!'))
        except Exception as e:
            logger.error(f"Erro ao treinar modelos de ML: {str(e)}")
            self.stdout.write(self.style.ERROR(f'Erro ao treinar modelos: {str(e)}'))