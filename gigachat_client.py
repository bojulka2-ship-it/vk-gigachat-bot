"""Лёгкий REST-клиент для GigaChat API.

Строит работу с API по официальной документации:
- получает access_token через OAuth 2.0 (client credentials) по POST /oauth;
- токен действует 30 минут и автоматически обновляется;
- генерирует ответы через POST /v2/chat/completions (поле content — массив
  объектов, в отличие от OpenAI-формата /v1).

Все секреты берутся только из переменных окружения (.env), в код не записываются.
"""

import base64
import logging
import os
import time
import uuid

import requests

logger = logging.getLogger(__name__)

GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
# Внимание: для доступа к чат-генерации используется эндпоинт /v2/chat/completions.
# В нём поле content каждого сообщения — это МАССИВ объектов [{"text": "..."}],
# а ответ приходит в поле messages[].content[].text (а не choices[].message.content).
GIGACHAT_API_URL = "https://api.giga.chat/v2/chat/completions"


class GigachatClientError(Exception):
    """Ошибка при работе с GigaChat API."""


class GigachatClient:
    """Клиент для генерации ответов через GigaChat API."""

    def __init__(self) -> None:
        self.client_id: str = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
        self.client_secret: str = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
        self.scope: str = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        self.model: str = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max").strip()
        self.verify_ssl: bool = (
            os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "false").strip().lower() == "true"
        )

        if not self.client_secret:
            raise ValueError(
                "Не задан GIGACHAT_CLIENT_SECRET. Заполните файл .env по образцу .env.example"
            )

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _session_headers(self) -> dict:
        """Заголовки, общие для всех запросов к API."""
        return {
            "Accept": "application/json",
            "User-Agent": "VK-GigaChat-Bot/1.0",
        }

    def _get_access_token(self) -> str:
        """Возвращает действующий access_token, при необходимости обновляя его."""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        # GigaChat может требовать и CLIENT_ID, и CLIENT_SECRET.
        # Если CLIENT_ID не задан, пробуем авторизоваться секретом напрямую.
        if self.client_id:
            credentials_raw = f"{self.client_id}:{self.client_secret}"
        else:
            credentials_raw = self.client_secret

        encoded = base64.b64encode(credentials_raw.encode("utf-8")).decode("ascii")

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {encoded}",
            "RqUID": str(uuid.uuid4()),
            "User-Agent": "VK-GigaChat-Bot/1.0",
        }
        logger.info("Запрашиваю новый access_token у GigaChat API")
        try:
            response = requests.post(
                GIGACHAT_AUTH_URL,
                headers=headers,
                data=f"scope={self.scope}",
                timeout=30,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GigachatClientError(
                "Не удалось получить токен доступа к GigaChat API. "
                "Проверьте GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET."
            ) from exc

        payload = response.json()
        self._access_token = payload["access_token"]
        # expires_at — unix-время в секундах, когда токен перестанет действовать.
        self._token_expires_at = float(payload.get("expires_at", 0)) or (time.time() + 1800)
        return self._access_token

    def chat(self, messages: list[dict]) -> str:
        """Отправляет список сообщений в GigaChat и возвращает текст ответа.

        messages — список словарей вида [{"role": "system"|"user"|"assistant",
                                          "content": "текст"}].
        """
        token = self._get_access_token()
        headers = {
            **self._session_headers(),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload_messages = [
            {"role": item["role"], "content": [{"text": item["content"]}]}
            for item in messages
        ]
        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": 0.7,
            "max_tokens": 800,
        }

        try:
            response = requests.post(
                GIGACHAT_API_URL,
                headers=headers,
                json=payload,
                timeout=60,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.exception("Ошибка запроса к GigaChat API")
            raise GigachatClientError(
                "GigaChat API временно недоступен или вернул ошибку. Попробуйте позже."
            ) from exc

        try:
            content = response.json()["messages"][0]["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GigachatClientError(
                "GigaChat API вернул ответ в неожиданном формате."
            ) from exc

        return content.strip()
