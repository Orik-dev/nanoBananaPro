CREDITS_PER_GENERATION = 1
CREDITS_PER_GENERATION_PRO = 5  # ✅ ДОБАВЛЕНО

PACKS_RUB = [149, 299, 690, 990,1900]

PACKS_CREDITS: dict[int, int] = {
    149: 30,
    299: 65,
    690: 170,
    990: 270,
    1900: 540,  # ✅ НОВЫЙ пакет
}

def credits_for_rub(rub: int) -> int:
    return PACKS_CREDITS.get(rub, 0)

def credits_per_generation(model: str = "standard") -> int:
    """Возвращает стоимость генерации для модели"""
    if model == "pro":
        return CREDITS_PER_GENERATION_PRO
    return CREDITS_PER_GENERATION