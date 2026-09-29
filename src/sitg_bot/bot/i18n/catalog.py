import html
import json
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo

DEFAULT_LOCALE = "ru"
SUPPORTED_LOCALES = ("ru", "en")
LOCALE_NAMES = {
    "ru": {"ru": "Русский", "en": "Russian"},
    "en": {"ru": "Английский", "en": "English"},
}


class CatalogError(ValueError):
    pass


class _TelegramHTMLValidator(HTMLParser):
    allowed_tags = {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
    }
    link_schemes = ("http://", "https://")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = attrs[0][1] if len(attrs) == 1 and attrs[0][0] == "href" else None
            if not href or not href.startswith(self.link_schemes):
                raise CatalogError(
                    "Translated links must use exactly one http(s) href attribute"
                )
            self.stack.append(tag)
            return
        if tag not in self.allowed_tags or attrs:
            raise CatalogError(f"Unsupported translated HTML element: {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            raise CatalogError(f"Unbalanced translated HTML element: {tag}")

    def finish(self) -> None:
        if self.stack:
            raise CatalogError(f"Unclosed translated HTML element: {self.stack[-1]}")


class LocalizationService:
    """Loads and validates canonical bot catalogs and safely formats HTML messages."""

    def __init__(self, catalogs: dict[str, dict[str, str]] | None = None) -> None:
        self.catalogs = catalogs or self._load_catalogs()
        self.validate()

    def locale(self, value: str | None) -> str:
        return value if value in self.catalogs else DEFAULT_LOCALE

    def text(self, key: str, locale: str, **parameters: object) -> str:
        selected = self.locale(locale)
        try:
            template = self.catalogs[selected][key]
        except KeyError as error:
            raise CatalogError(f"Unknown message key: {key}") from error
        escaped = {
            name: html.escape(str(value), quote=True) if isinstance(value, str) else value
            for name, value in parameters.items()
        }
        try:
            return template.format(**escaped)
        except KeyError as error:
            raise CatalogError(f"Missing parameter {error.args[0]!r} for {key}") from error

    def plural(self, key: str, locale: str, count: int, **parameters: object) -> str:
        selected = self.locale(locale)
        if selected == "ru":
            absolute = abs(count) % 100
            last = absolute % 10
            form = "one" if last == 1 and absolute != 11 else "few"
            if not (2 <= last <= 4 and not 12 <= absolute <= 14) and form != "one":
                form = "many"
        else:
            form = "one" if count == 1 else "other"
        candidate = f"{key}.{form}"
        if candidate not in self.catalogs[selected]:
            candidate = f"{key}.other"
        return self.text(candidate, selected, count=count, **parameters)

    def format_number(self, value: int | float, locale: str) -> str:
        rendered = f"{value:,}"
        if self.locale(locale) == "ru":
            return rendered.replace(",", " ").replace(".", ",")
        return rendered

    def format_datetime(self, value: datetime, locale: str, *, timezone: str = "UTC") -> str:
        localized = value.astimezone(ZoneInfo(timezone))
        pattern = "%d.%m.%Y %H:%M" if self.locale(locale) == "ru" else "%Y-%m-%d %H:%M"
        return localized.strftime(pattern)

    def validate(self) -> None:
        missing_locales = set(SUPPORTED_LOCALES) - self.catalogs.keys()
        if missing_locales:
            raise CatalogError(f"Missing catalogs: {', '.join(sorted(missing_locales))}")
        baseline = set(self.catalogs[DEFAULT_LOCALE])
        for locale in SUPPORTED_LOCALES:
            keys = set(self.catalogs[locale])
            if keys != baseline:
                missing = baseline - keys
                extra = keys - baseline
                raise CatalogError(
                    f"Catalog {locale} key mismatch; "
                    f"missing={sorted(missing)}, extra={sorted(extra)}"
                )
            for key, value in self.catalogs[locale].items():
                if not value.strip():
                    raise CatalogError(f"Catalog {locale} has an empty value for {key}")
                expected = self._fields(self.catalogs[DEFAULT_LOCALE][key])
                if self._fields(value) != expected:
                    raise CatalogError(f"Catalog placeholders differ for {key}")
                validator = _TelegramHTMLValidator()
                validator.feed(value)
                validator.close()
                validator.finish()

    @staticmethod
    def _fields(template: str) -> set[str]:
        return {name for _, name, _, _ in Formatter().parse(template) if name is not None}

    @staticmethod
    def _load_catalogs() -> dict[str, dict[str, str]]:
        directory = Path(__file__).with_name("locales")
        return {
            locale: json.loads((directory / f"{locale}.json").read_text(encoding="utf-8"))
            for locale in SUPPORTED_LOCALES
        }
