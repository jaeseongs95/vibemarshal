
from contextlib import contextmanager

body_calls = []


def command_migrate():
    body_calls.append("plain")
    return None


@contextmanager
def open_readonly():
    body_calls.append("generator")
    yield None


def fake_wrapper(*args, **kwargs):
    body_calls.append("fake")
    return None
