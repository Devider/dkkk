"""Self-contained Russian Snowball stemmer.

Pure-Python port of the Snowball Russian stemming algorithm
(https://snowballstem.org/algorithms/russian/stemmer.html), previously provided
by ``nltk.stem.snowball.RussianStemmer``.  NLTK was removed as a dependency:
the algorithm is rule-based, needs no corpora, and produces byte-for-byte the
same output as the NLTK implementation.

The internal transliteration scheme (Cyrillic <=> Roman) mirrors the one used
by NLTK so that suffix matching and vowel detection behave identically.

    >>> russian_stem("оборотные")
    'оборотн'
    >>> russian_stem("меди")
    'мед'
"""

from functools import lru_cache

_VOWELS = ("A", "U", "E", "a", "e", "i", "o", "u", "y")

_CYRILLIC_TO_ROMAN = (
    ("А", "a"),
    ("а", "a"),
    ("Б", "b"),
    ("б", "b"),
    ("В", "v"),
    ("в", "v"),
    ("Г", "g"),
    ("г", "g"),
    ("Д", "d"),
    ("д", "d"),
    ("Е", "e"),
    ("е", "e"),
    ("Ё", "e"),
    ("ё", "e"),
    ("Ж", "zh"),
    ("ж", "zh"),
    ("З", "z"),
    ("з", "z"),
    ("И", "i"),
    ("и", "i"),
    ("Й", "i`"),
    ("й", "i`"),
    ("К", "k"),
    ("к", "k"),
    ("Л", "l"),
    ("л", "l"),
    ("М", "m"),
    ("м", "m"),
    ("Н", "n"),
    ("н", "n"),
    ("О", "o"),
    ("о", "o"),
    ("П", "p"),
    ("п", "p"),
    ("Р", "r"),
    ("р", "r"),
    ("С", "s"),
    ("с", "s"),
    ("Т", "t"),
    ("т", "t"),
    ("У", "u"),
    ("у", "u"),
    ("Ф", "f"),
    ("ф", "f"),
    ("Х", "kh"),
    ("х", "kh"),
    ("Ц", "t^s"),
    ("ц", "t^s"),
    ("Ч", "ch"),
    ("ч", "ch"),
    ("Ш", "sh"),
    ("ш", "sh"),
    ("Щ", "shch"),
    ("щ", "shch"),
    ("Ъ", "''"),
    ("ъ", "''"),
    ("Ы", "y"),
    ("ы", "y"),
    ("Ь", "'"),
    ("ь", "'"),
    ("Э", "e`"),
    ("э", "e`"),
    ("Ю", "i^u"),
    ("ю", "i^u"),
    ("Я", "i^a"),
    ("я", "i^a"),
)

_ROMAN_TO_CYRILLIC = (
    ("i^u", "ю"),
    ("i^a", "я"),
    ("shch", "щ"),
    ("kh", "х"),
    ("t^s", "ц"),
    ("ch", "ч"),
    ("e`", "э"),
    ("i`", "й"),
    ("sh", "ш"),
    ("k", "к"),
    ("e", "е"),
    ("zh", "ж"),
    ("a", "а"),
    ("b", "б"),
    ("v", "в"),
    ("g", "г"),
    ("d", "д"),
    ("z", "з"),
    ("i", "и"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("f", "ф"),
    ("''", "ъ"),
    ("y", "ы"),
    ("'", "ь"),
)

# Suffix groups in the transliterated (Roman) alphabet, matching NLTK's tables.
_PERFECTIVE_GERUND_SUFFIXES = (
    "ivshis'",
    "yvshis'",
    "vshis'",
    "ivshi",
    "yvshi",
    "vshi",
    "iv",
    "yv",
    "v",
)
_ADJECTIVAL_SUFFIXES = (
    "ui^ushchi^ui^u",
    "ui^ushchi^ai^a",
    "ui^ushchimi",
    "ui^ushchymi",
    "ui^ushchego",
    "ui^ushchogo",
    "ui^ushchemu",
    "ui^ushchomu",
    "ui^ushchikh",
    "ui^ushchykh",
    "ui^ushchui^u",
    "ui^ushchaia",
    "ui^ushchoi^u",
    "ui^ushchei^u",
    "i^ushchi^ui^u",
    "i^ushchi^ai^a",
    "ui^ushchee",
    "ui^ushchie",
    "ui^ushchye",
    "ui^ushchoe",
    "ui^ushchei`",
    "ui^ushchii`",
    "ui^ushchyi`",
    "ui^ushchoi`",
    "ui^ushchem",
    "ui^ushchim",
    "ui^ushchym",
    "ui^ushchom",
    "i^ushchimi",
    "i^ushchymi",
    "i^ushchego",
    "i^ushchogo",
    "i^ushchemu",
    "i^ushchomu",
    "i^ushchikh",
    "i^ushchykh",
    "i^ushchui^u",
    "i^ushchai^a",
    "i^ushchoi^u",
    "i^ushchei^u",
    "i^ushchee",
    "i^ushchie",
    "i^ushchye",
    "i^ushchoe",
    "i^ushchei`",
    "i^ushchii`",
    "i^ushchyi`",
    "i^ushchoi`",
    "i^ushchem",
    "i^ushchim",
    "i^ushchym",
    "i^ushchom",
    "shchi^ui^u",
    "shchi^ai^a",
    "ivshi^ui^u",
    "ivshi^ai^a",
    "yvshi^ui^u",
    "yvshi^ai^a",
    "shchimi",
    "shchymi",
    "shchego",
    "shchogo",
    "shchemu",
    "shchomu",
    "shchikh",
    "shchykh",
    "shchui^u",
    "shchai^a",
    "shchoi^u",
    "shchei^u",
    "ivshimi",
    "ivshymi",
    "ivshego",
    "ivshogo",
    "ivshemu",
    "ivshomu",
    "ivshikh",
    "ivshykh",
    "ivshui^u",
    "ivshai^a",
    "ivshoi^u",
    "ivshei^u",
    "yvshimi",
    "yvshymi",
    "yvshego",
    "yvshogo",
    "yvshemu",
    "yvshomu",
    "yvshikh",
    "yvshykh",
    "yvshui^u",
    "yvshai^a",
    "yvshoi^u",
    "yvshei^u",
    "vshi^ui^u",
    "vshi^ai^a",
    "shchee",
    "shchie",
    "shchye",
    "shchoe",
    "shchei`",
    "shchii`",
    "shchyi`",
    "shchoi`",
    "shchem",
    "shchim",
    "shchym",
    "shchom",
    "ivshee",
    "ivshie",
    "ivshye",
    "ivshoe",
    "ivshei`",
    "ivshii`",
    "ivshyi`",
    "ivshoi`",
    "ivshem",
    "ivshim",
    "ivshym",
    "ivshom",
    "yvshee",
    "yvshie",
    "yvshye",
    "yvshoe",
    "yvshei`",
    "yvshii`",
    "yvshyi`",
    "yvshoi`",
    "yvshem",
    "yvshim",
    "yvshym",
    "yvshom",
    "vshimi",
    "vshymi",
    "vshego",
    "vshogo",
    "vshemu",
    "vshomu",
    "vshikh",
    "vshykh",
    "vshui^u",
    "vshai^a",
    "vshoi^u",
    "vshei^u",
    "emi^ui^u",
    "emi^ai^a",
    "nni^ui^u",
    "nni^ai^a",
    "vshee",
    "vshie",
    "vshye",
    "vshoe",
    "vshei`",
    "vshii`",
    "vshyi`",
    "vshoi`",
    "vshem",
    "vshim",
    "vshym",
    "vshom",
    "emimi",
    "emymi",
    "emego",
    "emogo",
    "ememu",
    "emomu",
    "emikh",
    "emykh",
    "emui^u",
    "emai^a",
    "emoi^u",
    "emei^u",
    "nnimi",
    "nnymi",
    "nnego",
    "nnogo",
    "nnemu",
    "nnomu",
    "nnikh",
    "nnykh",
    "nnui^u",
    "nnai^a",
    "nnoi^u",
    "nnei^u",
    "emee",
    "emie",
    "emye",
    "emoe",
    "emei`",
    "emii`",
    "emyi`",
    "emoi`",
    "emem",
    "emim",
    "emym",
    "emom",
    "nnee",
    "nnie",
    "nnye",
    "nnoe",
    "nnei`",
    "nnii`",
    "nnyi`",
    "nnoi`",
    "nnem",
    "nnim",
    "nnym",
    "nnom",
    "i^ui^u",
    "i^ai^a",
    "imi",
    "ymi",
    "ego",
    "ogo",
    "emu",
    "omu",
    "ikh",
    "ykh",
    "ui^u",
    "ai^a",
    "oi^u",
    "ei^u",
    "ee",
    "ie",
    "ye",
    "oe",
    "ei`",
    "ii`",
    "yi`",
    "oi`",
    "em",
    "im",
    "ym",
    "om",
)
# Adjectival suffixes that are only removed when preceded by "а" or "я".
_ADJECTIVAL_PRECEDER_SUFFIXES = frozenset(
    (
        "i^ushchi^ui^u",
        "i^ushchi^ai^a",
        "i^ushchui^u",
        "i^ushchai^a",
        "i^ushchoi^u",
        "i^ushchei^u",
        "i^ushchimi",
        "i^ushchymi",
        "i^ushchego",
        "i^ushchogo",
        "i^ushchemu",
        "i^ushchomu",
        "i^ushchikh",
        "i^ushchykh",
        "shchi^ui^u",
        "shchi^ai^a",
        "i^ushchee",
        "i^ushchie",
        "i^ushchye",
        "i^ushchoe",
        "i^ushchei`",
        "i^ushchii`",
        "i^ushchyi`",
        "i^ushchoi`",
        "i^ushchem",
        "i^ushchim",
        "i^ushchym",
        "i^ushchom",
        "vshi^ui^u",
        "vshi^ai^a",
        "shchui^u",
        "shchai^a",
        "shchoi^u",
        "shchei^u",
        "emi^ui^u",
        "emi^ai^a",
        "nni^ui^u",
        "nni^ai^a",
        "shchimi",
        "shchymi",
        "shchego",
        "shchogo",
        "shchemu",
        "shchomu",
        "shchikh",
        "shchykh",
        "vshui^u",
        "vshai^a",
        "vshoi^u",
        "vshei^u",
        "shchee",
        "shchie",
        "shchye",
        "shchoe",
        "shchei`",
        "shchii`",
        "shchyi`",
        "shchoi`",
        "shchem",
        "shchim",
        "shchym",
        "shchom",
        "vshimi",
        "vshymi",
        "vshego",
        "vshogo",
        "vshemu",
        "vshomu",
        "vshikh",
        "vshykh",
        "emui^u",
        "emai^a",
        "emoi^u",
        "emei^u",
        "nnui^u",
        "nnai^a",
        "nnoi^u",
        "nnei^u",
        "vshee",
        "vshie",
        "vshye",
        "vshoe",
        "vshei`",
        "vshii`",
        "vshyi`",
        "vshoi`",
        "vshem",
        "vshim",
        "vshym",
        "vshom",
        "emimi",
        "emymi",
        "emego",
        "emogo",
        "ememu",
        "emomu",
        "emikh",
        "emykh",
        "nnimi",
        "nnymi",
        "nnego",
        "nnogo",
        "nnemu",
        "nnomu",
        "nnikh",
        "nnykh",
        "emee",
        "emie",
        "emye",
        "emoe",
        "emei`",
        "emii`",
        "emyi`",
        "emoi`",
        "emem",
        "emim",
        "emym",
        "emom",
        "nnee",
        "nnie",
        "nnye",
        "nnoe",
        "nnei`",
        "nnii`",
        "nnyi`",
        "nnoi`",
        "nnem",
        "nnim",
        "nnym",
        "nnom",
    )
)
_REFLEXIVE_SUFFIXES = ("si^a", "s'")
_VERB_SUFFIXES = (
    "esh'",
    "ei`te",
    "ui`te",
    "ui^ut",
    "ish'",
    "ete",
    "i`te",
    "i^ut",
    "nno",
    "ila",
    "yla",
    "ena",
    "ite",
    "ili",
    "yli",
    "ilo",
    "ylo",
    "eno",
    "i^at",
    "uet",
    "eny",
    "it'",
    "yt'",
    "ui^u",
    "la",
    "na",
    "li",
    "em",
    "lo",
    "no",
    "et",
    "ny",
    "t'",
    "ei`",
    "ui`",
    "il",
    "yl",
    "im",
    "ym",
    "en",
    "it",
    "yt",
    "i^u",
    "i`",
    "l",
    "n",
)
# Verb suffixes that are only removed when preceded by "а" or "я".
_VERB_PRECEDER_SUFFIXES = frozenset(
    ("la", "na", "ete", "i`te", "li", "i`", "l", "em", "n", "lo", "no", "et", "i^ut", "ny", "t'", "esh'", "nno")
)
_NOUN_SUFFIXES = (
    "ii^ami",
    "ii^akh",
    "i^ami",
    "ii^am",
    "i^akh",
    "ami",
    "iei`",
    "i^am",
    "iem",
    "akh",
    "ii^u",
    "'i^u",
    "ii^a",
    "'i^a",
    "ev",
    "ov",
    "ie",
    "'e",
    "ei",
    "ii",
    "ei`",
    "oi`",
    "ii`",
    "em",
    "am",
    "om",
    "i^u",
    "i^a",
    "a",
    "e",
    "i",
    "i`",
    "o",
    "u",
    "y",
    "'",
)
_SUPERLATIVE_SUFFIXES = ("ei`she", "ei`sh")
_DERIVATIONAL_SUFFIXES = ("ost'", "ost")


def _cyrillic_to_roman(word: str) -> str:
    for cyr, roman in _CYRILLIC_TO_ROMAN:
        word = word.replace(cyr, roman)
    return word


def _regions(word: str) -> tuple[str, str]:
    """Return the RV and R2 regions used by the Russian Snowball algorithm."""
    collapsed = word.replace("i^a", "A").replace("i^u", "U").replace("e`", "E")
    r1 = ""
    for i in range(1, len(collapsed)):
        if collapsed[i] not in _VOWELS and collapsed[i - 1] in _VOWELS:
            r1 = collapsed[i + 1 :]
            break
    r2 = ""
    for i in range(1, len(r1)):
        if r1[i] not in _VOWELS and r1[i - 1] in _VOWELS:
            r2 = r1[i + 1 :]
            break
    rv = ""
    for i in range(len(collapsed)):
        if collapsed[i] in _VOWELS:
            rv = collapsed[i + 1 :]
            break
    return (
        rv.replace("A", "i^a").replace("U", "i^u").replace("E", "e`"),
        r2.replace("A", "i^a").replace("U", "i^u").replace("E", "e`"),
    )


def _preceded_by_ye(candidate: str) -> bool:
    """Check whether the stem immediately before the suffix ends in а or я."""
    return candidate.endswith("i^a") or candidate.endswith("a")


def _strip(rv: str, suffix: str, require_preceded: bool = False) -> str | None:
    """Return the matching suffix to strip from *rv*, or None if no match."""
    if not rv.endswith(suffix):
        return None
    if require_preceded and not _preceded_by_ye(rv[: -len(suffix)]):
        return None
    return suffix


def _match(rv: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        if rv.endswith(suffix):
            return suffix
    return None


def _match_preceded(rv: str, suffixes: tuple[str, ...], preceded: frozenset[str]) -> str | None:
    for suffix in suffixes:
        matched = _strip(rv, suffix, require_preceded=suffix in preceded)
        if matched is not None:
            return matched
    return None


def _cut(roman: str, rv: str, r2: str, suffix: str) -> tuple[str, str, str]:
    length = len(suffix)
    return roman[:-length], rv[:-length], r2[:-length]


def _step1(roman: str, rv: str, r2: str) -> tuple[str, str, str]:
    perfect = _match_preceded(rv, _PERFECTIVE_GERUND_SUFFIXES, frozenset(("v", "vshi", "vshis'")))
    if perfect is not None:
        return _cut(roman, rv, r2, perfect)

    reflexive = _match(rv, _REFLEXIVE_SUFFIXES)
    if reflexive is not None:
        roman, rv, r2 = _cut(roman, rv, r2, reflexive)

    adjectival = _match_preceded(rv, _ADJECTIVAL_SUFFIXES, _ADJECTIVAL_PRECEDER_SUFFIXES)
    if adjectival is not None:
        return _cut(roman, rv, r2, adjectival)

    verb = _match_preceded(rv, _VERB_SUFFIXES, _VERB_PRECEDER_SUFFIXES)
    if verb is not None:
        return _cut(roman, rv, r2, verb)

    noun = _match(rv, _NOUN_SUFFIXES)
    if noun is not None:
        return _cut(roman, rv, r2, noun)

    return roman, rv, r2


def _step2(roman: str, rv: str, r2: str) -> tuple[str, str]:
    if rv.endswith("i"):
        return roman[:-1], r2[:-1]
    return roman, r2


def _step3(roman: str, r2: str) -> str:
    for suffix in _DERIVATIONAL_SUFFIXES:
        if r2.endswith(suffix):
            return roman[: -len(suffix)]
    return roman


def _step4(roman: str) -> str:
    if roman.endswith("nn"):
        return roman[:-1]

    superlative_removed = False
    for suffix in _SUPERLATIVE_SUFFIXES:
        if roman.endswith(suffix):
            roman = roman[: -len(suffix)]
            superlative_removed = True
            break
    if roman.endswith("nn"):
        roman = roman[:-1]

    if not superlative_removed and roman.endswith("'"):
        roman = roman[:-1]
    return roman


@lru_cache(maxsize=8192)
def russian_stem(word: str) -> str:
    """Stem a single Russian word using the Snowball algorithm.

    Words without any Cyrillic letters (e.g. ``"LME"``) are returned unchanged,
    matching the behaviour of ``RussianStemmer``.
    """
    if not any(ord(char) > 255 for char in word):
        return word

    roman = _cyrillic_to_roman(word)
    rv, r2 = _regions(roman)

    roman, rv, r2 = _step1(roman, rv, r2)
    roman, r2 = _step2(roman, rv, r2)
    roman = _step3(roman, r2)
    roman = _step4(roman)

    return _roman_to_cyrillic(roman)


def _roman_to_cyrillic(word: str) -> str:
    for roman, cyr in _ROMAN_TO_CYRILLIC:
        word = word.replace(roman, cyr)
    return word
