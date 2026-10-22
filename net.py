"""HTTP helper with retries and exponential backoff."""
import logging
import random
import time

import requests

import config

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": config.USER_AGENT, "Accept": "application/json, text/plain, */*"}
    )
    return session


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Send a request, retrying on network errors and 429/5xx responses."""
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT_SECONDS)
    last_exc: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            if 400 <= resp.status_code < 500:
                # Client errors will not fix themselves; fail fast.
                raise RuntimeError(f"HTTP {resp.status_code} for {url}")
            return resp
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == config.MAX_RETRIES:
                break
            wait = config.BACKOFF_BASE_SECONDS**attempt + random.uniform(0, 1)
            log.warning("Attempt %d/%d failed (%s). Retrying in %.1fs", attempt, config.MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Request failed after {config.MAX_RETRIES} attempts: {url}") from last_exc
