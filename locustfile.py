from locust import HttpUser, task, between

class UptimeMonitorUser(HttpUser):
    wait_time = between(0.01, 0.05)  # минимальная задержка между запросами для создания пиковой нагрузки

    @task(3)
    def get_websites(self):
        self.client.get("/api/websites")

    @task(1)
    def check_health(self):
        self.client.get("/docs")
