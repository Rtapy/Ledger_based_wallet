import codecs

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.utils import json


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class WalletJSONParser(JSONParser):
    """Do not silently choose one of two conflicting amount fields."""

    def parse(self, stream, media_type=None, parser_context=None):
        encoding = (parser_context or {}).get("encoding", settings.DEFAULT_CHARSET)
        try:
            return json.load(
                codecs.getreader(encoding)(stream),
                object_pairs_hook=_unique_object,
                parse_constant=json.strict_constant,
            )
        except (ValueError, LookupError) as exc:
            raise ParseError("Invalid JSON body.") from exc
