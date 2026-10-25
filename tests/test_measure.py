import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fractal_harness.behavior import baseline, import_tests
from fractal_harness.cache import ClaimCache
from fractal_harness.measure import common_prefix, neighbour_sizes, stats, tokens_estimate
from fractal_harness.taskmap import HEADER, HOW_TO

from test_behavior import repo  # noqa: F401  (fixture)
from test_hook import _repo as claims_repo

STATIC = HEADER + "\n" + HOW_TO
UTIL = "def double(x):\n    return 2 * x\n\ndef triple(x):\n    return 3 * x\n"


def graph(repo: Path) -> Path:
    (repo / "pkg" / "util.py").write_text(UTIL)
    (repo / "pkg" / "orphan.py").write_text("def lonely():\n    return 0\n")
    import_tests(repo)
    baseline(repo)
    return repo


def test_common_prefix():
    assert common_prefix([]) == 0 and common_prefix(["abc"]) == 3 and common_prefix(["", "a"]) == 0
    assert common_prefix(["abcd", "abxd"]) == 2 and common_prefix(["same", "same", "same"]) == 4
    assert common_prefix(["abc", "abcdef", "abcx"]) == 3 and common_prefix(["a", "b"]) == 0


def test_tokens_estimate_rounds_up_to_whole_tokens():
    assert [tokens_estimate(n) for n in (0, 1, 4, 5, 8, 9)] == [0, 1, 1, 2, 2, 3]


def test_stats_size_hits_misses_and_the_shared_start(repo):
    root = graph(repo)
    prompts = ["Make `quad` handle negative numbers", "Give lonely a docstring in pkg/orphan.py", "update the marketing copy"]
    s = stats(root, prompts)
    hit1, hit2, miss = s["injections"]
    assert hit1["injected"] and hit2["injected"] and not miss["injected"]
    assert miss["chars"] == 0 and miss["tokens_estimate"] == 0
    assert hit1["tokens_estimate"] == -(-hit1["chars"] // 4)
    assert s["repeatable"] is True
    assert s["shared_start_chars"] >= len(STATIC) and 0 < s["shared_start_share"] <= 1
    only_one = stats(root, prompts[:1])
    assert only_one["shared_start_chars"] == 0 and only_one["shared_start_share"] == 0.0     # nothing to share with


def test_stats_counts_what_signatures_stand_in_for(repo):
    root = graph(repo)
    assert stats(root, ["Make `quad` handle negative numbers"])["neighbours"] == {"signature_chars": 0, "neighbour_source_chars": 0}
    sizes = neighbour_sizes(root, "Make `quad` handle negative numbers", capacity=1)
    assert sizes == {"signature_chars": len("def double(x)"), "neighbour_source_chars": len(UTIL)}
    by_stats = stats(root, ["Make `quad` handle negative numbers"], capacity=1)["neighbours"]
    assert by_stats == sizes and by_stats["signature_chars"] < by_stats["neighbour_source_chars"]


def test_stats_writes_no_telemetry(repo):
    root = graph(repo)
    c = ClaimCache(root)
    before = (len(c.store.events("context")), len(c.store.events("inject")))
    c.close()
    stats(root, ["Make `quad` handle negative numbers", "update the marketing copy"])
    c = ClaimCache(root)
    assert (len(c.store.events("context")), len(c.store.events("inject"))) == before
    c.close()


def test_stats_on_a_repo_without_a_store_creates_nothing(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    s = stats(tmp_path, ["what does f do in a.py?"])
    assert not s["injections"][0]["injected"] and s["neighbours"] == {"signature_chars": 0, "neighbour_source_chars": 0}
    assert not (tmp_path / ".fractal").exists()


def test_the_cli_prints_the_stats_as_json(repo, capsys):
    from fractal_harness.cli import main
    root = graph(repo)
    assert main(["--root", str(root), "injection", "Make `quad` handle negative numbers", "update the marketing copy"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [i["injected"] for i in out["injections"]] == [True, False] and out["repeatable"] is True


# ---- the injected text is a function of the repo and the prompt, not of the process ----------------------------

def _inject(root: Path, prompt: str, seed: int) -> str:
    code = ("import sys; from pathlib import Path; from fractal_harness.hook import context_for; "
            "sys.stdout.write(context_for(Path(sys.argv[1]), sys.argv[2], log=False) or '')")
    return subprocess.run([sys.executable, "-c", code, str(root), prompt], capture_output=True, text=True, check=True,
                          env={**os.environ, "PYTHONHASHSEED": str(seed)}).stdout


def test_the_task_map_is_identical_across_processes_and_hash_seeds(repo):
    root = graph(repo)
    for prompt in ("Make `quad` handle negative numbers", "Change double() in pkg/util.py to use shifts"):
        outs = {_inject(root, prompt, seed) for seed in (1, 2, 3, 4)}
        assert len(outs) == 1 and next(iter(outs)).startswith("TASK MAP")


def test_rules_and_facts_are_identical_across_processes_and_hash_seeds(tmp_path):
    root = claims_repo(tmp_path)
    c = ClaimCache(root)
    for i in range(8):                                         # more enforced rules than the cap, so selection is exercised
        r = c.put(f"rule number {i}: nothing prints", ["*.py"], kind="invariant",
                  probe={"type": "grep", "pattern": f"print{i}\\(", "paths": ["*.py"], "expect": "absent"})
        c.set_rule(r.id, "enforced")
    c.close()
    prompt = "what GPS distance filter do we use for battery?"
    outs = {_inject(root, prompt, seed) for seed in (1, 2, 3, 4)}
    assert len(outs) == 1
    text = next(iter(outs))
    assert "GPS distance filter is 5 metres" in text
    assert text.count("holds now") == 6                       # eight enforced rules, shown up to MAX_RULES


# ---- ties in ranking and creation time never leave the order to chance -------------------------------------------

def test_ranked_ties_are_broken_by_id(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    for w in ("aaa", "bbb", "ccc", "ddd", "eee"):
        c.put(f"alpha beta {w}", ["a.py"])
    ranked = c.store.ranked("alpha beta")
    groups: dict[float, list[str]] = {}
    for eid, score, _ in ranked:
        groups.setdefault(score, []).append(eid)
    assert any(len(g) > 1 for g in groups.values())            # the test is about real ties
    assert all(g == sorted(g) for g in groups.values())
    assert [s for _, s, _ in ranked] == sorted(s for _, s, _ in ranked)
    c.close()


def test_listing_order_breaks_creation_time_ties_by_id(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    ids = [c.put(f"claim {w}", ["a.py"]).id for w in ("one", "two", "three", "four")]
    with c.store.db:
        c.store.db.execute("UPDATE edges SET created_at = 1")
    assert [e.id for e in c.store.all()] == sorted(ids)
    c.close()


def test_the_shared_start_share_is_the_shared_chars_over_the_shortest_injection(monkeypatch):
    from fractal_harness import measure
    texts = {"p1": "abc" + "x" * 6, "p2": "abc" + "y" * 12, "p3": "", "p4": "abc" + "z" * 3}
    monkeypatch.setattr(measure, "context_for", lambda root, prompt, log=True, primed=False: texts[prompt])
    monkeypatch.setattr(measure, "_has_store", lambda root: False)
    s = measure.stats(Path("."), ["p1", "p2"])
    assert s["shared_start_chars"] == 3 and s["shared_start_share"] == 0.333                 # 3 / 9, rounded to three places
    s = measure.stats(Path("."), ["p1", "p2", "p3", "p4"])                                    # a miss does not count as an injection
    assert s["shared_start_chars"] == 3 and s["shared_start_share"] == 0.5                   # 3 / 6: the shortest hit, not the longest
    assert [i["injected"] for i in s["injections"]] == [True, True, False, True]


def test_stats_reports_what_the_session_start_costs_and_what_each_prompt_saves(repo):
    from fractal_harness.hook import session_text
    root = graph(repo)
    s = stats(root, ["Make `quad` handle negative numbers", "update the marketing copy"])
    hit, miss = s["injections"]
    assert 0 < hit["primed_chars"] < hit["chars"] and miss["primed_chars"] == 0
    assert hit["chars"] - hit["primed_chars"] >= len(HOW_TO)                     # at least the how-to is not repeated
    assert s["saved_per_prompt_chars"] == hit["chars"] - hit["primed_chars"]     # misses inject nothing, so save nothing
    assert s["session_chars"] == len(session_text("map")) > 0
    assert stats(root, ["update the marketing copy"])["saved_per_prompt_chars"] == 0
