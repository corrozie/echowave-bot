# main.py
"""
EchoWave Telegram bot (aiogram 3.x)
-----------------------------------
Один файл с:
- FSM сценариями
- ReplyKeyboardMarkup
- анти-спамом
- follow-up через 10 минут тишины
- логированием
- загрузкой токена из .env
"""

import asyncio
import logging
import os
import time
from contextlib import suppress
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from dotenv import load_dotenv

# =========================
# Конфиг и логирование
# =========================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("echowave_bot")

# =========================
# Константы и тексты
# =========================

# Этап 1: стартовый выбор
BTN_ALREADY_RELEASE = "Уже выпускаю треки"
BTN_JUST_STARTING = "Только начинаю"
BTN_LOOKING_LABEL = "Ищу лейбл"

START_CHOICES = [BTN_ALREADY_RELEASE, BTN_JUST_STARTING, BTN_LOOKING_LABEL]

# Этап 2: ветки
BRANCH_CHOICES = {
    BTN_ALREADY_RELEASE: [
        "Нет команды",
        "Не хватает аудитории",
        "Сложно собрать цельный проект",
        "Всё делаю сам и выгораю",
    ],
    BTN_JUST_STARTING: [
        "Уже есть стиль",
        "Пока ищу себя",
    ],
    BTN_LOOKING_LABEL: [
        "Свобода творчества",
        "Продюсирование",
        "Продвижение и выпуск",
        "Всё вместе",
    ],
}

# Этап 4: CTA
BTN_PROJECT_REVIEW = "Разбор проекта"
BTN_GET_GUIDE = "Получить гайд"

CTA_CHOICES = [BTN_PROJECT_REVIEW, BTN_GET_GUIDE]
POST_REVIEW_CHOICES = [BTN_GET_GUIDE, "/start"]

# Follow-up delay (10 минут)
FOLLOW_UP_DELAY_SECONDS = 600

# Анти-спам: повтор одного и того же текста слишком быстро
ANTI_SPAM_SECONDS = 1.2

UNIVERSAL_MESSAGE = (
    "Похоже, ты сейчас в точке, где многие артисты застревают.\n\n"
    "Когда есть идеи, но нет системы или команды.\n\n"
    "EchoWave как раз про это:\n"
    "— помогаем собрать релиз как арт-проект\n"
    "— берём продюсирование и стратегию\n"
    "— не загоняем в тренды\n\n"
    "Здесь музыка — это высказывание."
)

FOLLOW_UP_MESSAGE = (
    "Иногда пауза — это нормально.\n\n"
    "Но если ты чувствуешь, что в твоей музыке есть что-то большее,\n"
    "чем просто релизы — это стоит развивать.\n\n"
    "Можем спокойно разобрать твой проект и посмотреть, куда его можно привести."
)

# =========================
# FSM состояния
# =========================


class EchoWaveStates(StatesGroup):
    choosing_position = State()
    choosing_detail = State()
    choosing_cta = State()

    review_genre = State()
    review_releases = State()
    review_goal = State()


# =========================
# Вспомогательные функции
# =========================

router = Router()

# Хранилище последних сообщений для анти-спама: user_id -> (text, timestamp)
last_user_message: Dict[int, tuple[str, float]] = {}

# Хранилище задач follow-up: user_id -> asyncio.Task
followup_tasks: Dict[int, asyncio.Task] = {}


def make_keyboard(buttons: list[str], resize: bool = True) -> ReplyKeyboardMarkup:
    """Создает Reply-клавиатуру из списка кнопок."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=btn)] for btn in buttons],
        resize_keyboard=resize,
    )


def is_fast_duplicate(user_id: int, text: str) -> bool:
    """
    Простейший анти-спам:
    если тот же пользователь отправил тот же текст слишком быстро — игнорируем.
    """
    now = time.monotonic()
    prev = last_user_message.get(user_id)

    if prev:
        prev_text, prev_ts = prev
        if prev_text == text and (now - prev_ts) < ANTI_SPAM_SECONDS:
            return True

    last_user_message[user_id] = (text, now)
    return False


def reset_followup_timer(bot: Bot, chat_id: int, user_id: int) -> None:
    """Перезапускает таймер follow-up для пользователя."""
    old_task = followup_tasks.get(user_id)
    if old_task and not old_task.done():
        old_task.cancel()

    followup_tasks[user_id] = asyncio.create_task(
        send_followup_after_delay(bot=bot, chat_id=chat_id, user_id=user_id)
    )


async def send_followup_after_delay(bot: Bot, chat_id: int, user_id: int) -> None:
    """Отправляет follow-up, если пользователь молчит 10 минут."""
    try:
        await asyncio.sleep(FOLLOW_UP_DELAY_SECONDS)
        await bot.send_message(chat_id, FOLLOW_UP_MESSAGE)
        logger.info("Follow-up sent to user_id=%s", user_id)
    except asyncio.CancelledError:
        # Нормальное поведение: таймер сбрасывается при новом сообщении.
        pass
    except Exception as exc:
        logger.exception("Failed to send follow-up to user_id=%s: %s", user_id, exc)


def touch_user_activity(message: Message) -> None:
    """Фиксирует активность пользователя и обновляет follow-up таймер."""
    if not message.from_user:
        return
    reset_followup_timer(
        bot=message.bot,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
    )


# =========================
# Handlers: /start
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Старт: приветствие и выбор текущей точки пользователя."""
    if not message.from_user:
        return

    touch_user_activity(message)

    await state.clear()
    await state.set_state(EchoWaveStates.choosing_position)

    start_text = (
        "Привет.\n"
        "Это EchoWave.\n\n"
        "Мы работаем с музыкой как с искусством, а не контентом.\n\n"
        "Скажи, где ты сейчас:"
    )

    await message.answer(
        start_text,
        reply_markup=make_keyboard(START_CHOICES),
    )


# =========================
# Handlers: выбор позиции
# =========================


@router.message(EchoWaveStates.choosing_position, F.text.in_(START_CHOICES))
async def handle_position_choice(message: Message, state: FSMContext) -> None:
    """Обработка первого выбора и переход в соответствующую ветку."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    position = message.text
    await state.update_data(position=position)

    if position == BTN_ALREADY_RELEASE:
        text = "Понял.\nЧто сейчас больше всего тормозит тебя?"
    elif position == BTN_JUST_STARTING:
        text = "Ок.\nТы уже чувствуешь своё звучание или пока в поиске?"
    else:
        text = "Тогда главный вопрос:\nчто для тебя критично?"

    await state.set_state(EchoWaveStates.choosing_detail)
    await message.answer(
        text,
        reply_markup=make_keyboard(BRANCH_CHOICES[position]),
    )


@router.message(EchoWaveStates.choosing_position)
async def handle_position_invalid(message: Message) -> None:
    """Защита от некорректного ввода на шаге выбора позиции."""
    touch_user_activity(message)
    await message.answer("Выбери один из вариантов на клавиатуре.")


# =========================
# Handlers: выбор детали (ветка)
# =========================


@router.message(EchoWaveStates.choosing_detail)
async def handle_detail_choice(message: Message, state: FSMContext) -> None:
    """Универсальная обработка второго шага + переход к CTA."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    data = await state.get_data()
    position: Optional[str] = data.get("position")

    if not position or position not in BRANCH_CHOICES:
        await message.answer("Давай начнем сначала: /start")
        await state.clear()
        return

    allowed = BRANCH_CHOICES[position]
    if message.text not in allowed:
        await message.answer("Выбери один из вариантов на клавиатуре.")
        return

    await state.update_data(detail_answer=message.text)

    await message.answer(UNIVERSAL_MESSAGE)
    await state.set_state(EchoWaveStates.choosing_cta)
    await message.answer(
        "Если хочешь, можем разобрать твой проект.\nИли отправлю гайд.",
        reply_markup=make_keyboard(CTA_CHOICES),
    )


# =========================
# Handlers: CTA
# =========================


@router.message(EchoWaveStates.choosing_cta, F.text == BTN_PROJECT_REVIEW)
async def handle_cta_review(message: Message, state: FSMContext) -> None:
    """Переход в FSM-ветку разбора проекта (3 вопроса)."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await state.update_data(cta=BTN_PROJECT_REVIEW)
    await state.set_state(EchoWaveStates.review_genre)

    await message.answer(
        "Какой у тебя жанр?",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(EchoWaveStates.choosing_cta, F.text == BTN_GET_GUIDE)
async def handle_cta_guide(message: Message, state: FSMContext) -> None:
    """Отправка гайда (заглушка-ссылка)."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await state.update_data(cta=BTN_GET_GUIDE)

    await message.answer(
        "Отправляю гайд:\n"
        "«Почему твою музыку забывают за неделю и как это изменить?»\n\n"
        "Ссылка: https://example.com/echowave-guide"
    )

    # Сценарий завершен; состояние можно очистить.
    await state.clear()


@router.message(EchoWaveStates.choosing_cta)
async def handle_cta_invalid(message: Message) -> None:
    """Защита от некорректного ввода на шаге CTA."""
    touch_user_activity(message)
    await message.answer("Выбери один из вариантов: «Разбор проекта» или «Получить гайд».")


# =========================
# Handlers: Разбор проекта (FSM на 3 вопроса)
# =========================


@router.message(EchoWaveStates.review_genre)
async def review_genre_step(message: Message, state: FSMContext) -> None:
    """1/3: собираем жанр."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await state.update_data(review_genre=message.text)
    await state.set_state(EchoWaveStates.review_releases)
    await message.answer("Есть ли уже релизы?")


@router.message(EchoWaveStates.review_releases)
async def review_releases_step(message: Message, state: FSMContext) -> None:
    """2/3: собираем информацию о релизах."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await state.update_data(review_releases=message.text)
    await state.set_state(EchoWaveStates.review_goal)
    await message.answer("Какая сейчас цель?")


@router.message(EchoWaveStates.review_goal)
async def review_goal_step(message: Message, state: FSMContext) -> None:
    """3/3: собираем цель, логируем анкету и завершаем."""
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await state.update_data(review_goal=message.text)

    data = await state.get_data()
    user_id = message.from_user.id

    # Логируем собранную анкету для продюсера/CRM.
    logger.info(
        "Project review form | user_id=%s | position=%s | detail=%s | genre=%s | releases=%s | goal=%s",
        user_id,
        data.get("position"),
        data.get("detail_answer"),
        data.get("review_genre"),
        data.get("review_releases"),
        data.get("review_goal"),
    )

    await message.answer(
        "Принял.\n"
        "Передаю продюсеру EchoWave.\n"
        "С тобой свяжутся."
    )
    await message.answer(
        "Пока ждёшь ответ, могу отправить гайд.\n"
        "Или можешь перезапустить сценарий.",
        reply_markup=make_keyboard(POST_REVIEW_CHOICES),
    )

    await state.clear()


@router.message(F.text == BTN_GET_GUIDE)
async def handle_guide_anywhere(message: Message, state: FSMContext) -> None:
    """
    Универсальная отправка гайда:
    работает и после анкеты, и из любого другого места сценария.
    """
    if not message.from_user or not message.text:
        return

    if is_fast_duplicate(message.from_user.id, message.text):
        return

    touch_user_activity(message)

    await message.answer(
        "Отправляю гайд:\n"
        "«Почему твою музыку забывают за неделю и как это изменить?»\n\n"
        "Ссылка: https://example.com/echowave-guide"
    )
    await state.clear()


# =========================
# Fallback handler
# =========================


@router.message()
async def fallback_handler(message: Message) -> None:
    """Fallback: мягко возвращаем пользователя в сценарий."""
    touch_user_activity(message)
    await message.answer("Напиши /start, и начнем.")


# =========================
# Точка входа
# =========================


async def main() -> None:
    """Инициализация и запуск long-polling."""
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(router)

    logger.info("EchoWave bot is starting...")
    try:
        await dp.start_polling(bot)
    finally:
        # Корректно отменяем все pending follow-up задачи при остановке.
        for task in followup_tasks.values():
            if not task.done():
                task.cancel()
        with suppress(Exception):
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
