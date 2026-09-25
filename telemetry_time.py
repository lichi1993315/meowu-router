"""UTC telemetry timestamps, including .NET's seven-digit round-trip fractions."""
import re
from datetime import datetime


def parse_timestamp(value):
    """Normalize sub-microsecond precision consistently on Python 3.10 and 3.11+."""
    if not isinstance(value,str):
        raise ValueError('timestamp must be a string')
    value=re.sub(r'(\.\d{6})\d+',r'\1',value.replace('Z','+00:00'))
    result=datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timestamp timezone required')
    return result
