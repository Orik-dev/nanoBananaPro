from __future__ import annotations
import asyncio
import json
import logging
import time
from vendors.kie_rate_limiter import kie_rate_limiter
from typing import Any, Dict, List, Optional

import httpx

from core.config import settings

log = logging.getLogger("kie")


class KieError(Exception):
    ...


def _j(event: str, **fields) -> str:
    return json.dumps({"event": event, **fields}, ensure_ascii=False)


class KieClient:
    """
    KIE AI Client для работы с google/nano-banana и google/nano-banana-edit
    
    ✅ ИСПРАВЛЕНА ОБРАБОТКА RATE LIMIT
    """
    def __init__(self):
        self.base = settings.KIE_BASE.rstrip("/")
        self.create_url = f"{self.base}/jobs/createTask"
        self.status_url = f"{self.base}/jobs/recordInfo"
        self.headers = {
            "Authorization": f"Bearer {settings.KIE_API_KEY}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, read=90.0, connect=15.0)
        )

    async def aclose(self):
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def create_task(
        self,
        prompt: str,
        image_urls: Optional[List[str]] = None,
        callback_url: Optional[str] = None,
        *,
        output_format: Optional[str] = None,
        image_size: Optional[str] = None,
        user_model: str = "standard",  # ✅ ДОБАВЛЕНО
        cid: Optional[str] = None,
    ) -> str:
        """
        Создание задачи генерации с поддержкой Pro модели
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is empty")
        
        original_len = len(prompt)
        if len(prompt) > 2000:
            log.warning(_j(
                "kie.create.prompt_too_long",
                cid=cid,
                original_len=original_len,
                truncated_len=2000
            ))
            prompt = prompt[:2000]

        # ✅ ОБНОВЛЕНО: выбор модели в зависимости от параметра
        has_images = bool(image_urls)
        
        if user_model == "pro":
            model = settings.KIE_MODEL_PRO_EDIT if has_images else settings.KIE_MODEL_PRO_CREATE
        else:
            model = settings.KIE_MODEL_EDIT if has_images else settings.KIE_MODEL_CREATE

        payload: Dict[str, Any] = {
            "model": model,
            "input": {
                "prompt": prompt,
                "output_format": output_format or settings.KIE_OUTPUT_FORMAT,
                "image_size": image_size or settings.KIE_IMAGE_SIZE,
            }
        }

        if has_images:
            payload["input"]["image_urls"] = image_urls[:5]

        if callback_url:
            payload["callBackUrl"] = callback_url

        log.info(_j(
            "kie.create.request",
            cid=cid,
            model=model,
            user_model=user_model,  # ✅ ДОБАВЛЕНО
            urls=len(image_urls) if image_urls else 0,
            prompt_len=len(prompt),
            original_prompt_len=original_len
        ))
        
        await kie_rate_limiter.acquire()
        delay = 2.0
        max_attempts = 5
        
        for attempt in range(1, max_attempts + 1):
            try:
                r = await self._client.post(self.create_url, headers=self.headers, json=payload)
            except httpx.TimeoutException:
                if attempt < max_attempts:
                    log.warning(_j("kie.create.timeout", cid=cid, attempt=attempt))
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 30.0)
                    continue
                raise KieError("timeout")
            except Exception as e:
                if attempt < max_attempts:
                    log.warning(_j("kie.create.network_error", cid=cid, attempt=attempt, error=str(e)[:100]))
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 30.0)
                    continue
                raise KieError(f"network_error:{str(e)[:100]}")

            # ✅ 1. ОБРАБОТКА HTTP 429
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait_s = float(ra) if (ra and str(ra).replace('.', '').isdigit()) else delay
                
                log.warning(_j(
                    "kie.create.http_429",
                    cid=cid,
                    attempt=attempt,
                    retry_after=wait_s
                ))
                
                if attempt < max_attempts:
                    await asyncio.sleep(wait_s)
                    delay = min(delay * 2.0, 30.0)
                    continue
                else:
                    raise KieError("rate_limit_exceeded_http_429")

            # Парсинг ответа
            try:
                data = r.json()
            except Exception:
                data = {"code": r.status_code, "message": r.text}

            # ✅ 2. ГЛАВНОЕ ИСПРАВЛЕНИЕ: ОБРАБОТКА RATE LIMIT В ТЕЛЕ ОТВЕТА
            if r.status_code == 200:
                msg = str(data.get("message") or data.get("msg") or "").lower()
                
                # Проверяем различные формулировки rate limit
                rate_limit_indicators = [
                    "frequency is too high",
                    "try again later",
                    "rate limit",
                    "too many requests",
                    "call frequency",
                ]
                
                is_rate_limited = any(indicator in msg for indicator in rate_limit_indicators)
                
                if is_rate_limited:
                    wait_s = delay
                    log.warning(_j(
                        "kie.create.rate_limit_in_body",
                        cid=cid,
                        attempt=attempt,
                        retry_after=wait_s,
                        msg=msg[:200]
                    ))
                    
                    if attempt < max_attempts:
                        await asyncio.sleep(wait_s)
                        delay = min(delay * 2.0, 30.0)
                        continue
                    else:
                        raise KieError(f"rate_limit_exceeded:{msg[:200]}")

            # ✅ 3. ОБРАБОТКА 5XX
            if 500 <= r.status_code < 600:
                # Проверяем, что это именно Cloudflare/KIE ошибка
                is_cloudflare_error = "cloudflare" in (r.text or "").lower() or "kie.ai" in (r.text or "").lower()
                
                log.error(_j(
                    "kie.create.5xx",
                    cid=cid,
                    status=r.status_code,
                    is_cloudflare=is_cloudflare_error,
                    body=(r.text or "")[:400],
                    attempt=attempt
                ))
                
                if attempt < max_attempts:
                    # ✅ Для 5xx ошибок делаем более длинную задержку
                    wait_time = delay * 2 if is_cloudflare_error else delay
                    log.warning(_j(
                        "kie.create.5xx_retry",
                        cid=cid,
                        attempt=attempt,
                        wait_time=wait_time
                    ))
                    await asyncio.sleep(wait_time)
                    delay = min(delay * 2.0, 30.0)
                    continue
                else:
                    # ✅ Более понятное сообщение об ошибке
                    if is_cloudflare_error:
                        raise KieError("upstream_unavailable:cloudflare_error")
                    else:
                        raise KieError("upstream_5xx")

            # ✅ 4. ПРОВЕРКА УСПЕШНОСТИ
            code = int(data.get("code", 0))
            if r.status_code != 200 or code != 200:
                msg = (data.get("message") or data.get("msg") or r.text or "failed")[:200]
                log.error(_j(
                    "kie.create.bad_response",
                    cid=cid,
                    status=r.status_code,
                    code=code,
                    msg=msg
                ))
                raise KieError(f"bad_request:{msg}")

            # ✅ 5. УСПЕХ
            task_id = (data.get("data") or {}).get("taskId")
            if not task_id:
                raise KieError("no_task_id")

            log.info(_j(
                "kie.create.ok",
                cid=cid,
                task_id=task_id,
                model=model,
                attempt=attempt
            ))
            return task_id

        raise KieError("max_retries_exceeded")

    async def get_status(
        self,
        task_id: str,
        *,
        cid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Получение статуса задачи
        """
        max_attempts = 3
        delay = 2.0
        
        for attempt in range(1, max_attempts + 1):
            try:
                r = await self._client.get(
                    self.status_url,
                    headers=self.headers,
                    params={"taskId": task_id}
                )
            except Exception as e:
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 15.0)
                    continue
                raise KieError(f"network_error:{str(e)[:100]}")

            # ✅ Обработка HTTP 429
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait_s = float(ra) if (ra and str(ra).replace('.', '').isdigit()) else delay
                
                log.warning(_j(
                    "kie.status.http_429",
                    cid=cid,
                    task_id=task_id,
                    attempt=attempt,
                    retry_after=wait_s
                ))
                
                if attempt < max_attempts:
                    await asyncio.sleep(wait_s)
                    delay = min(delay * 2.0, 15.0)
                    continue
                else:
                    raise KieError("rate_limit_exceeded")

            try:
                data = r.json()
            except Exception:
                data = {"code": r.status_code, "message": r.text}

            # ✅ Проверка rate limit в теле ответа
            if r.status_code == 200:
                msg = str(data.get("message") or data.get("msg") or "").lower()
                
                if any(x in msg for x in ["frequency", "rate limit", "try again"]):
                    if attempt < max_attempts:
                        log.warning(_j(
                            "kie.status.rate_limit_in_body",
                            cid=cid,
                            task_id=task_id,
                            attempt=attempt
                        ))
                        await asyncio.sleep(delay)
                        delay = min(delay * 2.0, 15.0)
                        continue
                    else:
                        raise KieError("rate_limit_exceeded")

            code = int(data.get("code", 0))
            if r.status_code != 200 or code != 200:
                msg = (data.get("message") or data.get("msg") or r.text or "failed")[:200]
                log.error(_j(
                    "kie.status.bad_response",
                    cid=cid,
                    status=r.status_code,
                    code=code,
                    msg=msg
                ))
                raise KieError(f"status_failed:{msg}")

            # Парсинг результатов
            task_data = data.get("data") or {}
            state = str(task_data.get("state") or "").lower()

            result_urls: List[str] = []
            if state == "success":
                result_json = task_data.get("resultJson")
                if result_json:
                    try:
                        parsed = json.loads(result_json)
                        result_urls = parsed.get("resultUrls") or []
                    except Exception:
                        pass

            log.info(_j(
                "kie.status.ok",
                cid=cid,
                task_id=task_id,
                state=state,
                n=len(result_urls),
                attempt=attempt
            ))
            
            return {
                "state": state,
                "result_urls": result_urls,
                "fail_code": task_data.get("failCode"),
                "fail_msg": task_data.get("failMsg"),
                "raw": task_data
            }

        raise KieError("max_retries_exceeded")

    async def wait_until_done(
        self,
        task_id: str,
        timeout_s: int,
        *,
        cid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Ожидание завершения задачи
        """
        terminal = {"success", "fail"}
        start = time.time()
        delay = 2.0
        consecutive_rate_limits = 0

        while time.time() - start < timeout_s:
            try:
                d = await self.get_status(task_id, cid=cid)
                state = d.get("state")

                consecutive_rate_limits = 0

                if state in terminal:
                    log.info(_j(
                        "kie.done",
                        cid=cid,
                        task_id=task_id,
                        final_state=state
                    ))
                    return d

                await asyncio.sleep(delay)
                delay = min(delay + 0.5, 6.0)
                
            except KieError as e:
                error_str = str(e).lower()
                
                if "rate_limit" in error_str:
                    consecutive_rate_limits += 1
                    backoff = min(5.0 * (2 ** consecutive_rate_limits), 60.0)
                    
                    log.warning(_j(
                        "kie.wait.rate_limited",
                        cid=cid,
                        task_id=task_id,
                        consecutive=consecutive_rate_limits,
                        backoff=backoff
                    ))
                    
                    await asyncio.sleep(backoff)
                    continue
                
                raise

        raise KieError("timeout")