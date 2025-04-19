from django_celery_beat.models import PeriodicTask, IntervalSchedule
import json

# Criar ou obter o intervalo de 1 hora
schedule, created = IntervalSchedule.objects.get_or_create(
    every=1,
    period=IntervalSchedule.HOURS,
)

# Criar a tarefa periódica
PeriodicTask.objects.get_or_create(
    name='Treinar Modelos ML a Cada Hora',
    task='fila_online.tasks.treinar_modelos_periodicamente',
    interval=schedule,
    defaults={'enabled': True}
)