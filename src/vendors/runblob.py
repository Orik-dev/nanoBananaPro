from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from core.config import settings

log = logging.getLogger("runblob")


class RunBlobError(Exception):
    ...


def _j(event: str, **fields) -> str:
    return json.dumps({"event": event, **fields}, ensure_ascii=False)


def _summarize_images_for_log(images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, it in enumerate(images or []):
        if isinstance(it, dict) and isinstance(it.get("bytes"), str):
            out.append({"i": idx, "kind": "bytes_raw_b64", "has_mime": bool(it.get("mime")), "len": len(it["bytes"])})
        elif isinstance(it, dict) and isinstance(it.get("url"), str):
            out.append({"i": idx, "kind": "url", "len": len(it["url"])})
        else:
            out.append({"i": idx, "kind": "unknown"})
    return out


class RunBlobClient:
    def __init__(self):
        base = settings.RUNBLOB_BASE.rstrip("/")
        self.generate_url = f"{base}/generate"
        self.status_url = f"{base}/status"
        self.auth_hdr = {"Authorization": f"Bearer {settings.RUNBLOB_API_KEY}"}
        self.common_hdr = {**self.auth_hdr, "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=30.0, connect=10.0))

    async def aclose(self):
        try:
            await self._client.aclose()
        except Exception:
            pass

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.8, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def create_task(
        self,
        prompt: str,
        images: Optional[List[Dict[str, Any]]] = None,
        callback_url: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
        *,
        cid: Optional[str] = None,
    ) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is empty")

        payload: Dict[str, Any] = {"prompt": prompt}
        if aspect_ratio:
            payload["ar"] = aspect_ratio
        if seed is not None:  # ✅ ДОБАВЛЕНО
            payload["seed"] = seed    
        if images:
            payload["images"] = images  # [{"bytes":"<b64>", "mime":"image/jpeg"}]
        if callback_url:
            payload["callback_url"] = callback_url  # FIX

        # log.info(
        #     _j(
        #         "runblob.create_task.request",
        #         cid=cid,
        #         prompt_len=len(prompt),
        #         images_meta=_summarize_images_for_log(images or []),
        #         seed=seed,
        #         has_callback=bool(callback_url),
        #     )
        # )

        r = await self._client.post(self.generate_url, headers=self.common_hdr, json=payload)
        if r.status_code == 401:
            log.error(_j("runblob.create_task.unauthorized", cid=cid))
            raise RunBlobError("Unauthorized: invalid API key")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            delay = int(ra) if (ra and ra.isdigit()) else 3
            log.warning(_j("runblob.create_task.rate_limited", cid=cid, retry_after=delay))
            await asyncio.sleep(delay)
            r = await self._client.post(self.generate_url, headers=self.common_hdr, json=payload)
        if 500 <= r.status_code < 600:
            log.error(_j("runblob.create_task.5xx", cid=cid, status=r.status_code, resp=r.text[:500]))
        if r.status_code == 400:
            log.error(
                _j(
                    "runblob.create_task.bad_request",
                    cid=cid,
                    resp=r.text[:500],
                    images_meta=_summarize_images_for_log(images or []),
                )
            )
            raise RunBlobError(f"RunBlob rejected request (400): {r.text}")

        r.raise_for_status()
        data = r.json()
        task_uuid = data.get("task_uuid") or data.get("task_id") or data.get("id")
        if not task_uuid:
            log.error(_j("runblob.create_task.no_task_id", cid=cid, data=str(data)[:500]))
            raise RunBlobError(f"No task id in response: {data}")

        # log.info(_j("runblob.create_task.ok", cid=cid, task_uuid=task_uuid))
        return task_uuid

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.8, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def get_status(self, task_uuid: str, *, cid: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.status_url}/{task_uuid}"
        r = await self._client.get(url, headers=self.auth_hdr)
        if r.status_code == 401:
            log.error(_j("runblob.get_status.unauthorized", cid=cid, task_uuid=task_uuid))
            raise RunBlobError("Unauthorized: invalid API key")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            delay = int(ra) if (ra and ra.isdigit()) else 2
            log.warning(_j("runblob.get_status.rate_limited", cid=cid, retry_after=delay))
            await asyncio.sleep(delay)
            r = await self._client.get(url, headers=self.auth_hdr)
        r.raise_for_status()
        data = r.json()
        status = str(data.get("status", "")).lower()
        # log.info(_j("runblob.get_status.ok", cid=cid, task_uuid=task_uuid, status=status))
        return data

    async def wait_until_done(self, task_uuid: str, timeout_s: int, *, cid: Optional[str] = None) -> Dict[str, Any]:
        terminal = {"completed", "failed", "moderation_blocked", "error"}
        start = time.time()
        poll = 2.0
        while time.time() - start < timeout_s:
            data = await self.get_status(task_uuid, cid=cid)
            status = str(data.get("status", "")).lower()
            if status in terminal or not status:
                # log.info(_j("runblob.done", cid=cid, task_uuid=task_uuid, final_status=status))
                return data
            await asyncio.sleep(poll)
            poll = min(poll + 0.5, 6.0)
        log.error(_j("runblob.timeout", cid=cid, task_uuid=task_uuid, timeout_s=timeout_s))
        raise RunBlobError("Task timeout")
