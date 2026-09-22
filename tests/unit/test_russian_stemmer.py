"""Unit tests for the self-contained Russian Snowball stemmer.

Expected stems verified against ``nltk.stem.snowball.RussianStemmer``
(golden vectors captured from NLTK 3.9.4).
"""

import pytest

from aigw_service.api.v1.russian_stemmer import russian_stem

# (word, expected_stem) pairs produced by RussianStemmer
GOLDEN_VECTORS = [
    ("медь", "мед"),
    ("меди", "мед"),
    ("медных", "медн"),
    ("оборотные", "оборотн"),
    ("оборотный", "оборотн"),
    ("прочие", "проч"),
    ("прочих", "проч"),
    ("годовых", "годов"),
    ("добыча", "добыч"),
    ("себестоимость", "себестоим"),
    ("выручка", "выручк"),
    ("средства", "средств"),
    ("налог", "налог"),
    ("налоги", "налог"),
    ("покупка", "покупк"),
    ("продажа", "продаж"),
    ("инвестиции", "инвестиц"),
    ("капитальные", "капитальн"),
    ("амортизация", "амортизац"),
    ("запасы", "запас"),
    ("задолженность", "задолжен"),
    ("поступления", "поступлен"),
    ("расходы", "расход"),
    ("реализация", "реализац"),
    ("объем", "обь"),
    ("цена", "цен"),
    ("стоимость", "стоимост"),
    ("уменьшение", "уменьшен"),
    ("увеличение", "увеличен"),
    ("численность", "числен"),
    ("изменение", "изменен"),
    ("производство", "производств"),
    ("контракты", "контракт"),
    ("проценты", "процент"),
    ("дивиденды", "дивиденд"),
    ("затраты", "затрат"),
    ("активы", "актив"),
    ("обязательства", "обязательств"),
    ("электроэнергия", "электроэнерг"),
    ("трудовые", "трудов"),
    ("полученная", "получен"),
    ("здания", "здан"),
    ("права", "прав"),
]


class TestRussianStemmer:
    def test_golden_vectors(self):
        for word, expected in GOLDEN_VECTORS:
            assert russian_stem(word) == expected, f"{word!r} -> {expected!r}"

    def test_lowercase_invariance(self):
        assert russian_stem("Себестоимость") == russian_stem("себестоимость")
        assert russian_stem("ВЫРУЧКА") == russian_stem("выручка")

    @pytest.mark.parametrize(
        "word",
        ["LME", "EBITDA", "London Metals Exchange", "copper", "2026", "USD"],
    )
    def test_non_cyrillic_words_unchanged(self, word):
        assert russian_stem(word) == word

    def test_multiple_inflections_share_stem(self):
        stems = {russian_stem(word) for word in ("оборотные", "оборотный", "оборотной")}
        assert len(stems) == 1
