from collections import Counter

from fractal_harness import interfaces as I

PY = '''"""module doc"""
import os

@decorator
def fetch(url: str, *, retries: int = 3,
          timeout: float = 5.0) -> bytes:
    """Fetch it."""
    return b""

def _private(x):
    return x

class Client(Base):
    """A client."""
    async def send(self, payload: dict = {}) -> None:
        pass

def ab(x): return x
def dup(): pass
class Other:
    def dup(self): pass
'''

DART = '''import 'dart:async';

abstract class Player extends Base {
  final int id = 0;
  static const int maxVolume = 10;

  @override
  Future<void> play(Song song, {bool loop = false}) async {
    await _start(song);
  }

  Stream<PlayerState> get state => _controller.stream;
  bool get isEmpty => length == 0;
  void _hidden() {}
}

const String defaultName = 'x';
typedef Callback = void Function(int);
'''


def test_usage_counts_identifier_like_words_across_texts():
    c = I.usage(["a = fetch(x) + fetch(y)", "fetch.send(_p)"])
    assert c["fetch"] == 3 and c["send"] == 1 and c["_p"] == 1 and c["a"] == 1 and c["nope"] == 0
    assert I.usage([]) == Counter()


def test_defined_names_come_once_each_in_file_order():
    assert I.defined_names(PY) == ["fetch", "_private", "Client", "send", "ab", "dup", "Other"]
    assert I.defined_names(DART) == ["Player", "id", "maxVolume", "play", "state", "isEmpty", "_hidden", "defaultName", "Callback"]
    assert I.defined_names("") == [] and I.defined_names("print('x')\n") == []


def test_python_signatures_lose_decorators_docstrings_bodies_and_colons():
    assert I.signature(PY, "fetch") == "def fetch(url: str, *, retries: int = 3, timeout: float = 5.0) -> bytes"
    assert I.signature(PY, "Client") == "class Client(Base)"
    assert I.signature(PY, "send") == "async def send(self, payload: dict = {}) -> None"   # a `{}` default is inside the parentheses
    assert I.signature(PY, "ab") == "def ab(x)"                                              # a one-line body is not interface


def test_dart_signatures_cover_methods_getters_fields_and_types():
    assert I.signature(DART, "Player") == "abstract class Player extends Base"
    assert I.signature(DART, "play") == "Future<void> play(Song song, {bool loop = false}) async"   # the named-parameter brace is not the body
    assert I.signature(DART, "state") == "Stream<PlayerState> get state"                              # an expression body is dropped
    assert I.signature(DART, "isEmpty") == "bool get isEmpty"
    assert I.signature(DART, "id") == "final int id = 0"
    assert I.signature(DART, "maxVolume") == "static const int maxVolume = 10"
    assert I.signature(DART, "defaultName") == "const String defaultName = 'x'"
    assert I.signature(DART, "Callback") == "typedef Callback = void Function(int)"


def test_a_name_declared_twice_or_not_at_all_has_no_signature():
    assert I.signature(PY, "dup") is None                  # two declarations: not guessed
    assert I.signature(PY, "missing") is None and I.signature("", "x") is None


def test_long_signatures_are_cut_with_an_ellipsis_at_the_limit():
    params = ", ".join(f"argument_number_{i}: int" for i in range(20))
    sig = I.signature(f"def big({params}) -> None:\n    pass\n", "big")
    assert len(sig) == I.MAX_SIGNATURE_CHARS and sig.endswith("...") and sig.startswith("def big(argument_number_0: int,")


def test_a_signature_runaway_is_bounded_by_the_line_limit():
    text = "def open_forever(\n" + "    a,\n" * 50
    sig = I.signature(text, "open_forever")
    assert sig.count("a,") == I.MAX_SIGNATURE_LINES - 1


def test_interface_lines_keep_only_public_used_names_most_used_first():
    used = Counter({"fetch": 5, "Client": 2, "send": 2, "_private": 9, "ab": 4, "nothere": 7, "Other": 0})
    assert I.interface_lines(PY, used) == [
        "def fetch(url: str, *, retries: int = 3, timeout: float = 5.0) -> bytes",
        "class Client(Base)",                                   # ties keep file order: Client before send
        "async def send(self, payload: dict = {}) -> None",
    ]                                                           # `_private` is private, `ab` is too short, `Other` is unused


def test_interface_lines_respect_the_limit_and_skip_unresolvable_names():
    used = Counter({n: 1 for n in I.defined_names(DART)})
    assert len(I.interface_lines(DART, used)) == I.MAX_PER_FILE
    assert I.interface_lines(DART, used, limit=2) == ["abstract class Player extends Base", "static const int maxVolume = 10"]   # `id` is too short
    assert I.interface_lines(PY, Counter({"dup": 3, "fetch": 1})) == [I.signature(PY, "fetch")]   # `dup` is ambiguous
    assert I.interface_lines("", Counter({"x": 1})) == [] and I.interface_lines(PY, Counter()) == []
