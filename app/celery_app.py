# app/celery_app.py
from celery import Celery
import os
from dotenv import load_dotenv

load_dotenv()

redis_url = os.getenv('REDIS_URL')

celery_app = Celery(
    'video_tasks',
    broker=redis_url,
    backend=redis_url,
    broker_connection_retry_on_startup=True,
    include=['app.tasks']
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300
)