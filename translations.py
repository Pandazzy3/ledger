"""Language + currency helpers for Ledger."""

SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh": "中文 (Chinese)",
    "es": "Español (Spanish)",
    "fr": "Français (French)",
    "de": "Deutsch (German)",
}

SUPPORTED_CURRENCIES = {
    "USD": ("US Dollar", "$"),
    "EUR": ("Euro", "€"),
    "GBP": ("British Pound", "£"),
    "CNY": ("Chinese Yuan", "¥"),
    "JPY": ("Japanese Yen", "¥"),
    "INR": ("Indian Rupee", "₹"),
    "KRW": ("South Korean Won", "₩"),
    "RUB": ("Russian Ruble", "₽"),
    "BRL": ("Brazilian Real", "R$"),
    "MXN": ("Mexican Peso", "MX$"),
    "CAD": ("Canadian Dollar", "C$"),
    "AUD": ("Australian Dollar", "A$"),
    "CHF": ("Swiss Franc", "CHF"),
    "SEK": ("Swedish Krona", "kr"),
    "SGD": ("Singapore Dollar", "S$"),
    "HKD": ("Hong Kong Dollar", "HK$"),
    "AED": ("UAE Dirham", "د.إ"),
    "ZAR": ("South African Rand", "R"),
    "TRY": ("Turkish Lira", "₺"),
    "NGN": ("Nigerian Naira", "₦"),
    "KES": ("Kenyan Shilling", "KSh"),
    "GHS": ("Ghanaian Cedi", "₵"),
}


def get_currency_symbol(code):
    if not code:
        return "$"
    return SUPPORTED_CURRENCIES.get(code, (code, code))[1]


def get_currency_name(code):
    if not code:
        return "US Dollar"
    return SUPPORTED_CURRENCIES.get(code, (code, code))[0]


def get_language_name(code):
    return SUPPORTED_LANGUAGES.get(code, "English")