"""Time helpers based on the Code Tytor draft, with millisecond validation."""
import math
import re


def parse_duration(value: str, *, allow_zero: bool = False) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Укажи длительность: секунды, ММ:СС или ЧЧ:ММ:СС.')
    parts = value.strip().replace(',', '.').split(':')
    if len(parts) > 3:
        raise ValueError('Допустимы форматы: секунды, ММ:СС или ЧЧ:ММ:СС.')
    if any(not re.fullmatch(r'\d+', p) for p in parts[:-1]):
        raise ValueError('Часы и минуты должны быть целыми неотрицательными числами.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', parts[-1]):
        raise ValueError('Секунды должны быть неотрицательным числом.')
    try:
        nums = [float(p) for p in parts]
        if not all(math.isfinite(n) for n in nums):
            raise ValueError
        if len(nums) > 1 and any(n >= 60 for n in nums[-2:]):
            raise ValueError('В формате со знаком : минуты и секунды должны быть меньше 60.')
        total = sum(n * 60 ** i for i, n in enumerate(reversed(nums)))
        if not math.isfinite(total) or total < 0 or (total == 0 and not allow_zero):
            raise ValueError
    except (OverflowError, ValueError) as exc:
        if str(exc):
            raise ValueError(str(exc)) from None
        raise ValueError('Длительность должна быть конечным числом больше нуля.') from None
    return total


def format_duration(seconds: float) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError('Длительность должна быть числом.')
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Длительность должна быть конечным неотрицательным числом.')
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    suffix = f'.{ms:03d}'.rstrip('0') if ms else ''
    return f'{hours:02d}:{minutes:02d}:{secs:02d}{suffix}'


def duration_from_interval(start: str, end: str) -> float:
    """Return the length of a scene from its start and end timestamps."""
    start_seconds = parse_duration(start, allow_zero=True)
    end_seconds = parse_duration(end)
    if end_seconds <= start_seconds:
        raise ValueError('Время окончания должно быть позже времени начала.')
    return end_seconds - start_seconds
