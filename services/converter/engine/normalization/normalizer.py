"""Stage 9 — value normalisation.

Guiding rules (from the specification):

* the original text is always preserved next to the normalised value;
* documents/identifiers are never turned into numbers (leading zeros die);
* dates follow the locale detected in the document, otherwise the value is kept
  as text and the uncertainty is recorded;
* money is computed with `Decimal` to avoid binary float artefacts;
* text is data, never a formula: a cell starting with `=` is written as a
  string and flagged.

The normaliser never invents a value: if a string cannot be parsed it stays
text and confidence drops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

# --------------------------------------------------------------------------- #
# Locale
# --------------------------------------------------------------------------- #

_PT_NUMBER = re.compile(r"\b\d{1,3}(?:\.\d{3})+,\d{1,2}\b")
_EN_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\.\d{1,2}\b")
# Plain decimals without thousands grouping ("110.00" / "110,00").
# Without them an English invoice scores zero on both sides and silently
# falls back to the pt-BR default, turning 110.00 into 11000.
_PT_DECIMAL = re.compile(r"\b\d+,\d{1,2}\b(?!\d)")
_EN_DECIMAL = re.compile(r"\b\d+\.\d{1,2}\b(?!\d)")
_DATE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
_CURRENCY = re.compile(r"(R\$|US\$|€|£|\$)", re.IGNORECASE)
_PERCENT = re.compile(r"^\s*([-+]?[\d.,]+)\s*%\s*$")
_COMPETENCIA = re.compile(r"^\s*(0?[1-9]|1[0-2])/(\d{4})\s*$|^\s*(\d{4})-(0[1-9]|1[0-2])\s*$")

_IDENTIFIERS = (
    re.compile(r"^\d{3}\.\d{3}\.\d{3}-\d{2}$"),  # CPF
    re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$"),  # CNPJ
    re.compile(r"^\d{5}-?\d{3}$"),  # CEP
    re.compile(r"^\(?\d{2}\)?\s?9?\d{4}[- ]?\d{4}$"),  # telefone
    re.compile(r"^\d{8,}$"),  # matrícula / processo / código longo
    re.compile(r"^\d{4,}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}$"),  # processo CNJ
)

_IDENTIFIER_HEADER = re.compile(
    r"\b(cpf|cnpj|cep|matr[íi]cula|registro|documento|telefone|fone|celular|"
    r"c[óo]digo|inscri[çc][ãa]o|protocolo|processo|id|identificador|conta|ag[êe]ncia|"
    r"barra|boleto|nota|pedido|contrato|chave)\b",
    re.IGNORECASE,
)
_MONEY_HEADER = re.compile(
    r"\b(valor|sal[áa]rio|pre[çc]o|total|montante|receita|despesa|"
    r"custo|faturamento|b[ôo]nus|desconto|tributo|imposto|base|vencimento|"
    r"l[íi]quido|bruto|pagamento)\b",
    re.IGNORECASE,
)
_DATE_HEADER = re.compile(r"\b(data|nascimento|admiss[ãa]o|emiss[ãa]o|compet[êe]ncia)\b", re.IGNORECASE)

_MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


@dataclass(slots=True)
class LocaleProfile:
    decimal_sep: str = ","
    thousand_sep: str = "."
    date_order: str = "dmy"  # dmy | mdy | ymd
    confidence: float = 0.6
    samples: int = 0

    @classmethod
    def default(cls) -> "LocaleProfile":
        return cls()


def detect_locale(texts: list[str]) -> LocaleProfile:
    joined = "\n".join(texts[:5000])
    pt = len(_PT_NUMBER.findall(joined)) + len(_PT_DECIMAL.findall(joined))
    en = len(_EN_NUMBER.findall(joined)) + len(_EN_DECIMAL.findall(joined))

    first_greater = 0
    second_greater = 0
    for match in _DATE.finditer(joined):
        try:
            first, second = int(match.group(1)), int(match.group(2))
        except ValueError:
            continue
        if first > 12 and second <= 12:
            first_greater += 1
        elif second > 12 and first <= 12:
            second_greater += 1

    if first_greater > second_greater:
        order = "dmy"
    elif second_greater > first_greater:
        order = "mdy"
    else:
        order = "dmy"  # Brazilian product default

    if pt > en:
        decimal, thousand = ",", "."
        confidence = min(0.95, 0.6 + 0.05 * min(pt, 7))
    elif en > pt:
        decimal, thousand = ".", ","
        confidence = min(0.95, 0.6 + 0.05 * min(en, 7))
    else:
        decimal, thousand = ",", "."
        confidence = 0.5
    return LocaleProfile(
        decimal_sep=decimal,
        thousand_sep=thousand,
        date_order=order,
        confidence=confidence,
        samples=pt + en + first_greater + second_greater,
    )


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class NormalizedValue:
    value: object
    display: str
    value_type: str
    original: str
    number_format: str | None = None
    confidence: float = 1.0
    transformations: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.value_type == "empty"


EMPTY = NormalizedValue(value=None, display="", value_type="empty", original="", confidence=1.0)


def normalize(text: str | None, *, header: str | None = None, locale: LocaleProfile | None = None) -> NormalizedValue:
    locale = locale or LocaleProfile.default()
    original = "" if text is None else str(text)
    stripped = original.strip()
    if not stripped:
        return NormalizedValue(None, "", "empty", original, "General", 0.9)

    if _looks_like_formula(stripped):
        return NormalizedValue(
            value=stripped,
            display=stripped,
            value_type="text",
            original=original,
            number_format="@",
            confidence=0.9,
            transformations=[
                {
                    "rule": "formula_injection_guard",
                    "from": stripped,
                    "to": stripped,
                    "reason": "Texto iniciado por operador foi mantido como dado literal.",
                }
            ],
        )

    # -- header hints ------------------------------------------------------
    hint_type = _hint_from_header(header)

    # -- dates -------------------------------------------------------------
    parsed_date = _parse_date(stripped, locale)
    if parsed_date is not None and hint_type != "code":
        ambiguous = _is_ambiguous_date(stripped)
        return NormalizedValue(
            value=parsed_date,
            display=parsed_date.strftime("%d/%m/%Y"),
            value_type="date",
            original=original,
            number_format="dd/mm/yyyy",
            # An ambiguous date (both components <= 12) is a documented
            # assumption, not a certainty: it must not claim the same
            # confidence as "31/01/2026".
            confidence=0.70 if ambiguous else 0.88,
            transformations=[
                {
                    "rule": "date_parse",
                    "from": stripped,
                    "to": parsed_date.isoformat(),
                    "reason": (
                        f"Data ambígua interpretada na ordem {locale.date_order} do documento."
                        if ambiguous
                        else f"Interpretado como data ({locale.date_order})."
                    ),
                }
            ],
        )

    competencia = _parse_competencia(stripped)
    if competencia is not None and hint_type != "code":
        return NormalizedValue(
            value=competencia,
            display=competencia,
            value_type="competencia",
            original=original,
            number_format="@",
            confidence=0.85,
            transformations=[
                {"rule": "competencia", "from": stripped, "to": competencia, "reason": "MM/AAAA"}
            ],
        )

    # -- money -------------------------------------------------------------
    money = _parse_money(stripped, locale)
    if money is not None:
        amount, display, symbol, negative = money
        return NormalizedValue(
            value=float(-amount if negative else amount),
            display=("-" if negative else "") + display,
            value_type="money",
            original=original,
            number_format=f'"{symbol}" #,##0.00',
            confidence=0.93,
            transformations=[
                {
                    "rule": "money_parse",
                    "from": stripped,
                    "to": f"{-amount if negative else amount}",
                    "reason": f"Valor monetário normalizado com Decimal ({symbol}).",
                }
            ],
        )

    # -- percentage --------------------------------------------------------
    percent = _PERCENT.match(stripped)
    if percent and hint_type != "code":
        parsed = _parse_decimal(percent.group(1), locale)
        if parsed is not None:
            fraction = float(parsed / Decimal(100))
            return NormalizedValue(
                value=fraction,
                display=f"{_format_pt(parsed)}%",
                value_type="percentage",
                original=original,
                number_format="0.00%",
                confidence=0.9,
                transformations=[
                    {
                        "rule": "percentage_parse",
                        "from": stripped,
                        "to": str(fraction),
                        "reason": "Percentual convertido para fração.",
                    }
                ],
            )

    # -- identifiers -------------------------------------------------------
    # A long digit run under a monetary header ("Valor: 12345678") is a value:
    # the 8+ digit rule alone used to turn it into an identifier.
    long_digits_as_value = hint_type == "money" and stripped.isdigit()
    if not long_digits_as_value and (
        _is_identifier(stripped)
        or (hint_type == "code" and _only_digits_and_separators(stripped))
        or (stripped.isdigit() and _leading_zero(stripped))
    ):
        return NormalizedValue(
            value=stripped,
            display=stripped,
            value_type="code" if _leading_zero(stripped) else "identifier",
            original=original,
            number_format="@",
            confidence=0.95,
            transformations=[
                {
                    "rule": "identifier_preserved",
                    "from": stripped,
                    "to": stripped,
                    "reason": "Identificador mantido como texto (zeros à esquerda preservados).",
                }
            ],
        )

    # -- booleans ----------------------------------------------------------
    lowered = stripped.lower()
    if lowered in {"sim", "s", "true", "verdadeiro", "yes", "y"}:
        return NormalizedValue(True, stripped, "boolean", original, None, 0.8)
    if lowered in {"não", "nao", "n", "false", "falso", "no"}:
        return NormalizedValue(False, stripped, "boolean", original, None, 0.8)

    # -- numbers -----------------------------------------------------------
    if _only_digits_and_separators(stripped):
        decimal_value = _parse_decimal(stripped, locale)
        if decimal_value is not None:
            if hint_type == "money":
                # The header ("Salário", "Valor", "Total") says this column is
                # monetary even though the cell omits the symbol.
                return NormalizedValue(
                    value=float(decimal_value),
                    display=_format_pt(decimal_value),
                    value_type="money",
                    original=original,
                    number_format=f'"{_currency_symbol(stripped) or "R$"}" #,##0.00',
                    confidence=0.86,
                    transformations=[
                        {
                            "rule": "money_from_header",
                            "from": stripped,
                            "to": str(decimal_value),
                            "reason": f"Cabeçalho '{header}' indica valor monetário.",
                        }
                    ],
                )
            if decimal_value == decimal_value.to_integral_value() and not _has_decimal_part(
                stripped, locale
            ):
                integer = int(decimal_value)
                return NormalizedValue(
                    value=integer,
                    display=str(integer),
                    value_type="integer",
                    original=original,
                    number_format="#,##0",
                    confidence=0.92,
                    transformations=[
                        {"rule": "integer_parse", "from": stripped, "to": str(integer), "reason": "Número inteiro."}
                    ],
                )
            return NormalizedValue(
                value=float(decimal_value),
                display=_format_pt(decimal_value),
                value_type="decimal",
                original=original,
                number_format="#,##0.00",
                confidence=0.9,
                transformations=[
                    {
                        "rule": "decimal_parse",
                        "from": stripped,
                        "to": str(decimal_value),
                        "reason": "Número decimal normalizado.",
                    }
                ],
            )

    # -- text --------------------------------------------------------------
    return NormalizedValue(
        value=stripped,
        display=stripped,
        value_type="text",
        original=original,
        number_format="@",
        confidence=0.75,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _looks_like_formula(text: str) -> bool:
    if not text:
        return False
    if text.startswith("="):
        return True
    if text[0] in "+-@" and not _only_digits_and_separators(text):
        return True
    return False


def _hint_from_header(header: str | None) -> str | None:
    if not header:
        return None
    if _IDENTIFIER_HEADER.search(header):
        return "code"
    if _MONEY_HEADER.search(header):
        return "money"
    if _DATE_HEADER.search(header):
        return "date"
    return None


def _only_digits_and_separators(text: str) -> bool:
    return bool(re.fullmatch(r"[\d.,\s%+\-()R$€£]+", text)) and any(ch.isdigit() for ch in text)


def _leading_zero(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return len(digits) > 1 and digits.startswith("0")


def _is_identifier(text: str) -> bool:
    return any(pattern.match(text) for pattern in _IDENTIFIERS)


def _has_decimal_part(text: str, locale: LocaleProfile) -> bool:
    raw = text.replace(" ", "")
    separator = locale.decimal_sep
    if separator not in raw:
        return False
    tail = raw.rsplit(separator, 1)[1]
    return tail.isdigit() and 1 <= len(tail) <= 6 and not tail.endswith("000")


def _parse_decimal(text: str, locale: LocaleProfile) -> Decimal | None:
    raw = text.strip()
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    raw = raw.replace("\u00a0", " ").strip()
    # Strip currency symbols *and* their letter prefixes ("US$", "R$", "EUR").
    raw = re.sub(r"[A-Za-z$€£]", "", raw).strip()
    raw = raw.replace("%", "").strip()
    if raw.endswith("-"):
        negative = True
        raw = raw[:-1]
    if raw.startswith("-"):
        negative = True
        raw = raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]
    raw = raw.replace(" ", "")
    if not raw or not re.fullmatch(r"[\d.,]+", raw):
        return None
    if not any(ch.isdigit() for ch in raw):
        return None

    dec = locale.decimal_sep
    thou = locale.thousand_sep
    if dec in raw and thou in raw:
        if raw.rfind(dec) > raw.rfind(thou):
            normalized = raw.replace(thou, "").replace(dec, ".")
        else:
            normalized = raw.replace(dec, "").replace(thou, ".")
    elif dec in raw:
        occurrences = raw.count(dec)
        tail = raw.rsplit(dec, 1)[1]
        if occurrences > 1:
            groups = raw.split(dec)
            if len(groups[-1]) == 2 and all(len(g) == 3 for g in groups[1:-1]):
                normalized = "".join(groups[:-1]) + "." + groups[-1]
            else:
                normalized = raw.replace(dec, "")
        else:
            decimals = len(tail)
            if decimals == 0:
                normalized = raw.replace(dec, "")
            elif decimals <= 2:
                normalized = raw.replace(dec, ".")
            elif decimals == 3 and len(raw.split(dec)[0]) <= 3:
                # "1.250" is 1250 in pt-BR, 1.25 in en-US
                normalized = raw.replace(dec, "") if dec == thou else raw.replace(dec, ".")
            else:
                normalized = raw.replace(dec, ".")
    elif thou in raw:
        normalized = raw.replace(thou, "")
    else:
        normalized = raw

    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return -value if negative else value


def _parse_money(text: str, locale: LocaleProfile) -> tuple[Decimal, str, str, bool] | None:
    """Return (amount, display, currency_symbol, negative)."""
    if not (_CURRENCY.search(text) or _looks_accounting(text)):
        return None
    cleaned = text
    # "(350,00)" is the accounting negative convention documented in the plan
    # and covered by tests/test_normalization.py: it is applied by design.
    negative = text.strip().startswith("(") and text.strip().endswith(")")
    cleaned = cleaned.replace("(", "").replace(")", "")
    symbol = _currency_symbol(cleaned)
    had_currency = symbol is not None
    amount = _parse_decimal(cleaned, locale)
    if amount is None:
        return None
    if not had_currency and not negative and len(cleaned.split()) > 2:
        return None
    display = _format_pt(abs(amount))
    if had_currency:
        # The document's own symbol is preserved: rewriting US$ as R$ would
        # change the meaning of the value.
        display = f"{symbol} {display}"
    return abs(amount), display, symbol or "R$", negative or text.strip().startswith("-")


def _currency_symbol(text: str) -> str | None:
    """Currency symbol exactly as written in the document."""
    match = _CURRENCY.search(text)
    if not match:
        return None
    raw = match.group(1).upper().replace(" ", "")
    return raw if raw in {"R$", "US$", "€", "£"} else "$"


def _looks_accounting(text: str) -> bool:
    stripped = text.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return False
    return bool(re.fullmatch(r"\(\s*[\d.,\s]+\)", stripped))


def _format_pt(value: Decimal) -> str:
    if value != value:  # NaN
        return "NaN"
    with localcontext() as context:
        context.prec = max(28, len(str(int(abs(value)))) + 4)
        quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _is_ambiguous_date(text: str) -> bool:
    """True when both leading components can be a day and a month."""
    match = _DATE.fullmatch(text)
    if not match:
        return False
    try:
        first, second = int(match.group(1)), int(match.group(2))
    except ValueError:
        return False
    return 1 <= first <= 12 and 1 <= second <= 12


def _parse_date(text: str, locale: LocaleProfile) -> date | None:
    match = _DATE.fullmatch(text)
    if match:
        first, second, year = match.group(1), match.group(2), match.group(3)
        if len(year) == 2:
            year = f"20{year}" if int(year) < 70 else f"19{year}"
        try:
            a, b, y = int(first), int(second), int(year)
        except ValueError:
            return None
        if locale.date_order == "mdy":
            day, month = b, a
        else:
            day, month = a, b
        try:
            return date(y, month, day)
        except ValueError:
            return None
    # "15 de maio de 2026"
    named = re.fullmatch(r"(\d{1,2})\s+de\s+([a-zçãéíóúâêô]+)\s+de\s+(\d{4})", text.lower())
    if named:
        month = _MONTHS_PT.get(named.group(2))
        if month:
            try:
                return date(int(named.group(3)), month, int(named.group(1)))
            except ValueError:
                return None
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    return None


def _parse_competencia(text: str) -> str | None:
    match = _COMPETENCIA.fullmatch(text)
    if not match:
        return None
    if match.group(1):
        month, year = int(match.group(1)), match.group(2)
        return f"{month:02d}/{year}"
    month, year = match.group(4), match.group(3)
    return f"{month}/{year}"


def to_excel_value(normalized: NormalizedValue):
    value = normalized.value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return value


def canonical_header(name: str) -> str:
    text = " ".join((name or "").replace("\n", " ").split())
    text = text.strip(" :;.")
    return text[:255] if text else "Campo"


__all__ = [
    "NormalizedValue",
    "LocaleProfile",
    "detect_locale",
    "normalize",
    "to_excel_value",
    "canonical_header",
    "EMPTY",
]
