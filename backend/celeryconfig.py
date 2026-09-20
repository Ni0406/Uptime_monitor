import os

broker_url = os.getenv("CELERY_BROKER_URL", "amqp://admin:rabbitpassword@uptime-rabbitmq:5672//")
result_backend = os.getenv("CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://uptime-redis:6379/0"))

task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "UTC"
enable_utc = True

# Расписание Celery Beat
beat_schedule = {
    "ping-every-60-seconds": {
        "task": "ping_all_websites",
        "schedule": 60.0,
    },
}