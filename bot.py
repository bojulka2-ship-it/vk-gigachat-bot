"""Чат-бот сообщества ВКонтакте на базе GigaChat API.

Бот слушает входящие сообщения сообщества через Long Poll API,
передаёт их Алине (GigaChat) вместе с системным промптом и базой знаний
и отправляет ответ пользователю.

Запуск:  python bot.py
Зависит от файла .env, заполненного по образцу .env.example.
"""

import logging
import os
import random
import sys

import vk_api
from dotenv import load_dotenv
from vk_api.longpoll import VkEventType, VkLongPoll

from gigachat_client import GigachatClient, GigachatClientError
from prompt import get_system_prompt

# Максимальная длина сообщения ВКонтакте — 4096 символов (оставляем запас).
VK_MESSAGE_LIMIT = 4000

# Сколько последних сообщений диалога (пользователь + бот) помнить для Алины.
HISTORY_LIMIT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vk_giga_bot")


class VKGigaChatBot:
    """Обработка сообщений сообщества и генерация ответов через GigaChat."""

    def __init__(self) -> None:
        vk_token = os.getenv("VK_GROUP_TOKEN", "").strip()
        if not vk_token:
            raise ValueError(
                "Не задан VK_GROUP_TOKEN. Заполните файл .env по образцу .env.example"
            )

        self.system_prompt: str = get_system_prompt()
        self.giga: GigachatClient = GigachatClient()

        # История диалогов по пользователям:
        # user_id -> список {"role": "user"|"assistant", "content": "..."}
        self._history: dict[int, list[dict]] = {}

        self.vk_session = vk_api.VkApi(token=vk_token)
        self.longpoll = VkLongPoll(self.vk_session)
        self.vk = self.vk_session.get_api()

    # ------------------------------------------------------------------ #
    # Вспомогательные методы
    # ------------------------------------------------------------------ #
    def _get_history(self, user_id: int) -> list[dict]:
        """Возвращает историю диалога пользователя (или создаёт пустую)."""
        return self._history.setdefault(user_id, [])

    def _push_to_history(self, user_id: int, role: str, content: str) -> None:
        """Добавляет сообщение в историю и обрезает её до HISTORY_LIMIT."""
        history = self._get_history(user_id)
        history.append({"role": role, "content": content})
        del history[:-HISTORY_LIMIT]

    def _build_messages(self, user_id: int, user_text: str) -> list[dict]:
        """Собирает полный список сообщений для запроса к GigaChat."""
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(self._get_history(user_id))
        # Текущее сообщение пользователя добавляем отдельно, чтобы оно всегда
        # было последним в запросе.
        return messages + [{"role": "user", "content": user_text}]

    def _send_message(self, peer_id: int, text: str) -> None:
        """Отправляет сообщение в диалог пользователя."""
        text = text.strip() or "Извините, я не нашла, что ответить. Попробуйте переформулировать вопрос."
        if len(text) > VK_MESSAGE_LIMIT:
            text = text[: VK_MESSAGE_LIMIT - 3] + "..."

        self.vk.messages.send(
            peer_id=peer_id,
            message=text,
            random_id=random.randint(1, 2**31),
        )

    def _set_typing(self, peer_id: int) -> None:
        """Показывает пользователю индикатор «печатает...»."""
        try:
            self.vk.messages.setActivity(peer_id=peer_id, type="typing")
        except vk_api.exceptions.ApiError:
            logger.warning("Не удалось выставить статус «печатает»", exc_info=True)

    # ------------------------------------------------------------------ #
    # Основная логика
    # ------------------------------------------------------------------ #
    def _handle_message(self, user_id: int, peer_id: int, text: str) -> None:
        """Обрабатывает одно входящее сообщение и отправляет ответ."""
        original_text = text.strip()
        if not original_text:
            return

        self._set_typing(peer_id)
        logger.info("Сообщение от пользователя %s: %r", user_id, original_text[:120])

        try:
            messages = self._build_messages(user_id, original_text)
            reply = self.giga.chat(messages)
        except GigachatClientError as exc:
            logger.exception("Не удалось получить ответ от GigaChat")
            reply = str(exc)

        self._push_to_history(user_id, "user", original_text)
        self._push_to_history(user_id, "assistant", reply)
        self._send_message(peer_id, reply)

    def run(self) -> None:
        """Запускает бесконечный цикл прослушивания сообщений сообщества."""
        logger.info("Бот запущен и слушает сообщения сообщества ВКонтакте...")
        for event in self.longpoll.listen():
            try:
                if event.type != VkEventType.MESSAGE_NEW:
                    continue
                # Отвечаем только на сообщения, адресованные сообществу.
                if not event.to_me:
                    continue
                # Пропускаем любые вложения / нет текста.
                if not event.text:
                    continue

                user_id: int = event.user_id
                peer_id: int = event.peer_id
                self._handle_message(user_id, peer_id, event.text)
            except Exception:  # бот не должен падать на одном событии
                logger.exception("Ошибка при обработке события ВКонтакте")


def main() -> None:
    load_dotenv()
    try:
        bot = VKGigaChatBot()
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)
    bot.run()


if __name__ == "__main__":
    main()