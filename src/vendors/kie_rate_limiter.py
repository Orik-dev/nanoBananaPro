"""
Глобальный rate limiter для KIE AI
"""
import asyncio
import time
from typing import Optional
import logging

log = logging.getLogger("kie_rate_limiter")


class KieRateLimiter:
    """Контролирует частоту запросов к KIE AI"""
    def __init__(self, requests_per_second: float = 1.5):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time: Optional[float] = None
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        """Ждёт, пока не разрешится следующий запрос"""
        async with self.lock:
            if self.last_request_time is not None:
                elapsed = time.time() - self.last_request_time
                
                if elapsed < self.min_interval:
                    wait_time = self.min_interval - elapsed
                    await asyncio.sleep(wait_time)
            
            self.last_request_time = time.time()


# Глобальный экземпляр
kie_rate_limiter = KieRateLimiter(requests_per_second=1.5)