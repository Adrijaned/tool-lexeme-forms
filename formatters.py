import babel
import string


class PluralFormatter(string.Formatter):
    """A string formatter supporting a !p plural conversion.

    Format string examples:

        "I ate {count!p:0=no apples:one={count} apple:other={count} apples}."
        "{size} (0x{size:04X}) {size!p:one=bajt:two=bajtaj:few=bajty:other=bajtow}"

    For numeric values converted with !p, the format spec is
    interpreted differently: it consists of a set of key=text specs,
    separated by colons. The key should be one of the CLDR plural rule
    tags, currently “zero”, “one”, “two”, “few”, “many”, or “other”,
    or an explicit value. The text for the matching value or tag,
    according to the plural rules of the locale specified in the
    constructor, is substituted into the message. For all other
    values, the format spec is interpreted as usual.

    Note that most languages do not use all possible tags, and only
    exactly those tags used in a language should occur in the format
    string. For example, even though there is a “zero” tag, English
    only uses the “one” and “other” ones, and to make a special case
    for a value of zero with a PluralFormatter('en'), you need to use
    the key “0”, not “zero”. On the other hand, failing to specify all
    tags used in a language may make the formatter raise a KeyError:
    for instance, if the first example above used the key “1” instead
    of “one”, then it would fail when given a count of -1 or 1.0.

    Value keys always take precedence over tag keys, no matter in
    which order they are specified in the format spec. To match the
    value, they must be identical to the str() of the value: for
    instance, a “1” key will not match a 1.0 value or vice versa."""

    def __init__(self, locale_identifier):
        """The locale identifier must be understood by Locale.parse."""
        self.locale = babel.Locale.parse(locale_identifier)

    def convert_field(self, value, conversion):
        if conversion == 'p':
            return _Plural(value, self.locale)
        return super().convert_field(value, conversion)


class _Plural:
    """Wrapper around a numeric value with special formatting.

    This class formats itself as described in PluralFormatter."""

    def __init__(self, value, locale):
        self.value = value
        self.locale = locale

    def __format__(self, format_spec):
        plurals = format_spec.split(':')
        value_eq = str(self.value) + '='
        for plural in plurals:
            if plural.startswith(value_eq):
                return plural[len(value_eq):]
        tag = self.locale.plural_form(self.value)
        tag_eq = tag + '='
        for plural in plurals:
            if plural.startswith(tag_eq):
                return plural[len(tag_eq):]
        raise KeyError('No plural for tag {} found in format spec {}!'.format(tag, format_spec))
