#!/usr/bin/env python3
"""Замер токенов на реальных промптах ИИ-гида (Yandex Foundation Models).

Зачем: перед сменой модели нужно знать цену вопроса — сколько токенов уходит на
вход и сколько модель отдаёт на выход в КАЖДОМ из сценариев, которые прод реально
дёргает (рассказ, вопросы-подсказки, диалог, переписывание чисел для TTS).

Промпты не копируются, а импортируются из ``app/services/llm.py`` — иначе замер
разъедется с продом при первой же правке промпта.

Ходит в тот же OpenAI-совместимый шлюз, что и прод (``app/services/llm.py``):
``ai.api.cloud.yandex.net/v1/chat/completions``. Прежний ``foundationModels/v1``
знает только семейство ``yandexgpt`` и на deepseek/qwen/gpt-oss отвечает 404.

Отдельно показывает **reasoning-токены**: у deepseek-v4-flash «размышления»
включены по умолчанию, тарифицируются как выходные и при тесном ``max_tokens``
съедают весь бюджет — ответ приходит пустым с ``finish_reason=length``.

    export YANDEX_API_KEY=...            # либо YC_IAM_TOKEN=$(yc iam create-token)
    export YANDEX_FOLDER_ID=b1g...
    python scripts/llm_token_probe.py --model deepseek-v4-flash/latest

Опции:
    --model URI|SHORT   gpt://<folder>/<model> либо короткое имя (допишем folder)
    --reasoning EFFORT  none|low|minimal|… — прислать reasoning_effort
    --list              показать модели, доступные в каталоге, и выйти
    --json PATH         выгрузить сырые замеры
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Промпты — источник истины в сервисе, не здесь.
from app.config import settings  # noqa: E402
from app.services.llm import (  # noqa: E402
    _CHAT_SYSTEM,
    _SPOKEN_SYSTEM,
    _SPOKEN_USER_TMPL,
    _STYLE_HINT,
)

STORY_MAX_TOKENS = settings.llm_max_tokens_story

BASE = os.environ.get("LLM_API_BASE", "https://ai.api.cloud.yandex.net/v1")
COMPLETION_URL = f"{BASE}/chat/completions"
MODELS_URL = f"{BASE}/models"

STORY_SYSTEM = "Ты — ИИ-гид музея Фаберже."
QUESTIONS_SYSTEM = "Ты помогаешь придумать вопросы для диалога с гидом."

# Реальная справка из каталога (db/seed_fabergemuseum.sql, медианная длина
# raw_history ≈ 470 знаков). Короткая справка занижает вход, длинная завышает.
GROUNDING = (
    "Фирма: Карла Фаберже\n"
    "Место создания: Санкт-Петербург\n"
    "Дата: 1911\n"
    'Материалы: Золото, Рубин, Алмазы-"розы", Жемчуг, Перья, Нефрит, Аметисты, Цитрины\n'
    "Техника: Литье, Полировка, Чеканка, Гравировка, Эмаль\n"
    "Зал: Синяя гостиная\n"
    "Крона из резного нефрита усыпана цветами и плодами из самоцветов "
    "(аметисты, цитрины, рубины, алмазы).\n"
    "Сюрприз — поющая птичка, поднимающаяся из листвы при повороте механизма.\n"
    "Принадлежность к пасхальной серии подтверждена сохранившимся счётом Фаберже."
)

# Диалог, который бэкенд подмешивает в user-часть (llm.py: history[-6:]).
HISTORY = [
    ("user", "Расскажи об этом экспонате"),
    ("assistant", "Это пасхальное яйцо «Лавровое дерево», подарок Николая II матери в 1911 году."),
]

SPOKEN_SAMPLE = (
    "Яйцо «Лавровое дерево» создано в 1911 году по заказу Николая II для вдовствующей "
    "императрицы Марии Фёдоровны. Высота — около 30 см. Крона из нефрита украшена "
    "самоцветами, а внутри спрятан сюрприз — заводная птичка. Александр III начал "
    "традицию пасхальных подарков в 1885 году, к XIX веку ставшую семейной."
)


def _chat_user(message: str, grounding: str, history: List[Tuple[str, str]]) -> str:
    """Повторяет сборку user-части из ``llm._llm_chat``."""
    convo = "\n".join(f"{r}: {c}" for r, c in history[-6:])
    parts = []
    if grounding.strip():
        parts.append(f"Справка о текущем месте посетителя (может быть не связана с вопросом): {grounding}")
    if convo:
        parts.append(f"История диалога:\n{convo}")
    parts.append(f"Вопрос посетителя: {message}")
    return "\n".join(parts)


def _story_user(style: str = "engaging") -> str:
    return (
        f"Напиши интересную историю для посетителя музея, используя данные: {GROUNDING}, "
        f"стиль: {_STYLE_HINT[style]}, год: 1911."
    )


def _questions_user(n: int = 4) -> str:
    return (
        f"На основе данных об экспонате ({GROUNDING}) предложи {n} коротких вопроса, "
        "которые посетитель захотел бы задать гиду. Каждый вопрос с новой строки, без нумерации."
    )


# 10 запросов = реальный профиль трафика гида, а не 10 копий одного вопроса.
# temperature/maxTokens — ровно те, что стоят в llm.py для каждого вызова.
CASES: List[Dict] = [
    {"id": 1, "kind": "story", "note": "рассказ об экспонате (engaging)",
     "system": STORY_SYSTEM, "user": _story_user("engaging"), "temperature": 0.6, "max_tokens": STORY_MAX_TOKENS},
    {"id": 2, "kind": "story", "note": "рассказ, стиль short",
     "system": STORY_SYSTEM, "user": _story_user("short"), "temperature": 0.6, "max_tokens": STORY_MAX_TOKENS},
    {"id": 3, "kind": "questions", "note": "4 вопроса-подсказки",
     "system": QUESTIONS_SYSTEM, "user": _questions_user(4), "temperature": 0.7, "max_tokens": 200},
    {"id": 4, "kind": "chat", "note": "диалог: вопрос по справке, без истории",
     "system": _CHAT_SYSTEM, "user": _chat_user("Кто был мастером этого яйца?", GROUNDING, []),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 5, "kind": "chat", "note": "диалог: справка + история 2 реплики",
     "system": _CHAT_SYSTEM, "user": _chat_user("А что за сюрприз внутри?", GROUNDING, HISTORY),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 6, "kind": "chat", "note": "диалог: вопрос ВНЕ справки (баг-репорт 28.07)",
     "system": _CHAT_SYSTEM, "user": _chat_user("Расскажи про Петра I", GROUNDING, HISTORY),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 7, "kind": "chat", "note": "диалог без справки (общий чат)",
     "system": _CHAT_SYSTEM, "user": _chat_user("Чем знаменит Фаберже?", "", []),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 8, "kind": "chat", "note": "навигационный вопрос",
     "system": _CHAT_SYSTEM, "user": _chat_user("Как пройти в Рыцарский зал?", GROUNDING, []),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 9, "kind": "chat", "note": "длинная история (6 реплик — потолок окна)",
     "system": _CHAT_SYSTEM,
     "user": _chat_user("Сколько всего пасхальных яиц Фаберже?", GROUNDING, HISTORY * 3),
     "temperature": 0.6, "max_tokens": 800},
    {"id": 10, "kind": "spoken", "note": "числа прописью для TTS (temperature=0)",
     "system": _SPOKEN_SYSTEM, "user": _SPOKEN_USER_TMPL.format(text=SPOKEN_SAMPLE),
     "temperature": 0.0, "max_tokens": 2000},
]


def auth_headers() -> Dict[str, str]:
    # Шлюз OpenAI-совместимый: и API-ключ, и IAM-токен уходят как Bearer.
    secret = os.environ.get("YANDEX_API_KEY") or os.environ.get("YC_IAM_TOKEN")
    if not secret:
        sys.exit("ОШИБКА: задайте YANDEX_API_KEY или YC_IAM_TOKEN.")
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    folder = os.environ.get("YANDEX_FOLDER_ID")
    if folder:
        headers["OpenAI-Project"] = folder
    return headers


def resolve_model(raw: str) -> str:
    if raw.startswith("gpt://") or raw.startswith("ds://"):
        return raw
    folder = os.environ.get("YANDEX_FOLDER_ID")
    if not folder:
        sys.exit("ОШИБКА: короткое имя модели требует YANDEX_FOLDER_ID.")
    return f"gpt://{folder}/{raw}"


def request(url: str, headers: Dict[str, str], payload: Optional[Dict] = None,
            method: str = "POST", timeout: float = 120.0) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"ОШИБКА {exc.code} {exc.reason} на {url}\n{detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"ОШИБКА сети на {url}: {exc.reason}")


def build_payload(case: Dict, model_uri: str, reasoning: Optional[str]) -> Dict:
    payload = {
        "model": model_uri,
        "temperature": case["temperature"],
        "max_tokens": case["max_tokens"],
        "messages": [
            {"role": "system", "content": case["system"]},
            {"role": "user", "content": case["user"]},
        ],
    }
    if reasoning:
        payload["reasoning_effort"] = reasoning
    return payload


def run(model_uri: str, headers: Dict[str, str], reasoning: Optional[str]) -> List[Dict]:
    rows: List[Dict] = []
    for case in CASES:
        payload = build_payload(case, model_uri, reasoning)

        t0 = time.monotonic()
        data = request(COMPLETION_URL, headers, payload)
        latency = round(time.monotonic() - t0, 2)

        usage = data.get("usage", {})
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        # Reasoning тарифицируется как выходные токены, но в content не попадает,
        # поэтому «выход» без этой колонки выглядит необъяснимо большим.
        reasoning_chars = len(msg.get("reasoning_content") or "")

        rows.append({
            "id": case["id"],
            "kind": case["kind"],
            "note": case["note"],
            "chars_in": len(case["system"]) + len(case["user"]),
            "max_tokens": case["max_tokens"],
            "latency_s": latency,
            "usage_in": int(usage.get("prompt_tokens", 0)),
            "usage_out": int(usage.get("completion_tokens", 0)),
            "usage_total": int(usage.get("total_tokens", 0)),
            "finish_reason": choice.get("finish_reason", ""),
            "reasoning_chars": reasoning_chars,
            "chars_out": len(text),
            "text": text,
        })
        flag = "" if text.strip() else "  ← ПУСТОЙ ОТВЕТ"
        print(f"  [{case['id']:>2}/10] {case['kind']:<9} ok{flag}", file=sys.stderr)
    return rows


def report(rows: List[Dict], model_uri: str, reasoning: Optional[str]) -> None:
    print(f"\nМодель: {model_uri}")
    print(f"reasoning_effort: {reasoning or '(не отправлялся)'}")
    print(f"Запросов: {len(rows)}\n")

    head = (f"{'#':>2}  {'сценарий':<10} {'вход':>6} {'выход':>6} {'всего':>6} "
            f"{'лимит':>6} {'reas.зн':>8} {'сек':>5}  примечание")
    print(head)
    print("-" * len(head))
    for r in rows:
        cap = "!" if r["finish_reason"] == "length" else " "
        print(f"{r['id']:>2}  {r['kind']:<10} {r['usage_in']:>6} {r['usage_out']:>6}{cap}"
              f"{r['usage_total']:>6} {r['max_tokens']:>6} {r['reasoning_chars']:>8} "
              f"{r['latency_s']:>5}  {r['note']}")

    ins = [r["usage_in"] for r in rows]
    outs = [r["usage_out"] for r in rows]
    lat = [r["latency_s"] for r in rows]
    print(f"\nВход:  сумма {sum(ins):>6}  среднее {sum(ins)/len(ins):>6.0f}  "
          f"мин {min(ins)}  макс {max(ins)}")
    print(f"Выход: сумма {sum(outs):>6}  среднее {sum(outs)/len(outs):>6.0f}  "
          f"мин {min(outs)}  макс {max(outs)}")
    print(f"Итого: {sum(ins) + sum(outs)} токенов за {len(rows)} запросов")
    print(f"Латентность: среднее {sum(lat)/len(lat):.2f} с, макс {max(lat):.2f} с")

    empty = [r for r in rows if not r["text"].strip()]
    if empty:
        print("\nПУСТОЙ content — гид покажет посетителю пустой пузырь:")
        for r in empty:
            print(f"  #{r['id']} {r['note']}: finish_reason={r['finish_reason']}, "
                  f"выход {r['usage_out']}/{r['max_tokens']} ток., "
                  f"из них в reasoning {r['reasoning_chars']} знаков")

    truncated = [r for r in rows if r["finish_reason"] == "length" and r["text"].strip()]
    if truncated:
        print("\nОБРЕЗАНО ЛИМИТОМ max_tokens (ответ неполный):")
        for r in truncated:
            print(f"  #{r['id']} {r['note']}: {r['usage_out']}/{r['max_tokens']}")

    reas = sum(1 for r in rows if r["reasoning_chars"])
    if reas:
        print(f"\nReasoning сработал в {reas} из {len(rows)} запросов "
              f"(тарифицируется как выходные токены, в content не попадает).")


def list_models(headers: Dict[str, str]) -> None:
    data = request(MODELS_URL, headers, method="GET")
    for m in data.get("data", []):
        print(" ", m.get("id"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("YANDEXGPT_MODEL_URI", "yandexgpt/latest"),
                    help="gpt://<folder>/<model> или короткое имя")
    ap.add_argument("--reasoning", default=None,
                    help="reasoning_effort: none|low|minimal|… (по умолчанию не отправляется)")
    ap.add_argument("--list", action="store_true", help="показать модели каталога и выйти")
    ap.add_argument("--json", dest="json_out", help="куда сложить сырые замеры")
    args = ap.parse_args()

    headers = auth_headers()
    if args.list:
        list_models(headers)
        return

    model_uri = resolve_model(args.model)
    print(f"Прогон {len(CASES)} запросов по {model_uri}…", file=sys.stderr)
    rows = run(model_uri, headers, args.reasoning)
    report(rows, model_uri, args.reasoning)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"model": model_uri, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nСырые замеры: {args.json_out}")


if __name__ == "__main__":
    main()
