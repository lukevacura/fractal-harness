import csv
import io
import json

EXISTING = """date,amount,currency,category,description
2026-01-02,-30.00,EUR,food,Bakery
2026-01-05,-12.50,EUR,food,Lunch
"""


def _rows(text):
    return [r for r in csv.reader(io.StringIO(text)) if r]


def _import(ledger, tmp_path, fmt, content, existing=None):
    src = tmp_path / f"in.{fmt}"
    src.write_text(content)
    out = tmp_path / "out.csv"
    args = ["import", str(src), "--format", fmt, "--out", str(out)]
    if existing is not None:
        ex = tmp_path / "existing.csv"
        ex.write_text(existing)
        args += ["--existing", str(ex)]
    r = ledger(*args)
    return r, (_rows(out.read_text()) if out.exists() else [])


def test_bankxml_merges_dedupes_and_sorts(ledger, tmp_path):
    xml = ('<statement currency="EUR">'
           '<tx date="2026-01-09" amount="-4.20">  Coffee  </tx>'
           '<tx date="2026-01-05" amount="-12.50">  lunch </tx>'
           '<tx date="2026-01-03" amount="-8.00">Train</tx>'
           '</statement>')
    r, rows = _import(ledger, tmp_path, "bankxml", xml, EXISTING)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 2, skipped 1 duplicates"
    assert rows[0] == ["date", "amount", "currency", "category", "description"]
    assert rows[1:] == [["2026-01-02", "-30.00", "EUR", "food", "Bakery"],
                        ["2026-01-05", "-12.50", "EUR", "food", "Lunch"],
                        ["2026-01-03", "-8.00", "EUR", "uncategorized", "Train"],
                        ["2026-01-09", "-4.20", "EUR", "uncategorized", "Coffee"]]


def test_jsonexport_dates_and_amounts(ledger, tmp_path):
    doc = {"currency": "USD", "transactions": [
        {"booked": "05/02/2026", "value": -12.5, "memo": "Lunch"},
        {"booked": "01/02/2026", "value": 1234.5, "memo": "Salary"},
        {"booked": "01/02/2026", "value": 1234.5, "memo": "salary"}]}
    r, rows = _import(ledger, tmp_path, "jsonexport", json.dumps(doc))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 2, skipped 1 duplicates"
    assert rows[1:] == [["2026-02-01", "1234.50", "USD", "uncategorized", "Salary"],
                        ["2026-02-05", "-12.50", "USD", "uncategorized", "Lunch"]]


def test_semicolon_european_numbers(ledger, tmp_path):
    text = "Date;Amount;Currency;Text\n03.01.2026;-1.234,50;EUR;Rent January\n04.01.2026;2,00;EUR;Refund\n"
    r, rows = _import(ledger, tmp_path, "semicolon", text)
    assert r.returncode == 0, r.stderr
    assert rows[1:] == [["2026-01-03", "-1234.50", "EUR", "uncategorized", "Rent January"],
                        ["2026-01-04", "2.00", "EUR", "uncategorized", "Refund"]]


def test_malformed_input_fails(ledger, tmp_path):
    r, _ = _import(ledger, tmp_path, "semicolon", "Date;Amount;Currency;Text\nnot-a-date;x;EUR;?\n")
    assert r.returncode != 0


LEDGER = """date,amount,currency,category,description
2026-01-03,-9.50,EUR,uncategorized,Cafe lunch
2026-01-04,-900.00,EUR,uncategorized,Rent January
2026-01-05,3000.00,EUR,uncategorized,Salary
2026-01-06,-4.00,EUR,treats,cafe
"""

RULES = [{"category": "food", "pattern": "lunch|grocer", "priority": 1},
         {"category": "big", "pattern": ".", "max_amount": -500, "priority": 5},
         {"category": "coffee", "pattern": "cafe", "priority": 1}]


def _categorize(ledger, tmp_path, *extra):
    led, rules, out = tmp_path / "l.csv", tmp_path / "r.json", tmp_path / "o.csv"
    led.write_text(LEDGER)
    rules.write_text(json.dumps(RULES))
    r = ledger("categorize", str(led), "--rules", str(rules), "--out", str(out), *extra)
    return r, (_rows(out.read_text()) if out.exists() else [])


def test_categorize_priority_ties_and_ranges(ledger, tmp_path):
    r, rows = _categorize(ledger, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "categorized 2"
    assert [row[3] for row in rows[1:]] == ["food", "big", "uncategorized", "treats"]


def test_categorize_force(ledger, tmp_path):
    r, rows = _categorize(ledger, tmp_path, "--force")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "categorized 3"
    assert [row[3] for row in rows[1:]] == ["food", "big", "uncategorized", "coffee"]


RECURRING = """date,amount,currency,category,description
2026-01-05,-15.99,USD,subs,Netflix
2026-02-04,-15.99,USD,subs,NETFLIX
2026-03-06,-15.99,USD,subs,Netflix
2026-04-05,-15.99,USD,subs,netflix
2026-01-07,-9.99,USD,subs,Spotify
2026-02-06,-9.99,USD,subs,Spotify
2026-03-08,-12.99,USD,subs,Spotify
2026-01-10,-40.00,USD,health,Gym
2026-02-09,-40.00,USD,health,Gym
2026-01-11,-50.00,USD,food,Groceries
2026-01-16,-52.00,USD,food,Groceries
2026-01-21,-49.00,USD,food,Groceries
2026-01-31,3000.00,USD,salary,Employer
2026-03-02,3000.00,USD,salary,Employer
2026-04-01,3000.00,USD,salary,Employer
"""


def _recurring(ledger, tmp_path, *extra):
    led = tmp_path / "l.csv"
    led.write_text(RECURRING)
    return ledger("recurring", str(led), *extra)


def test_recurring_default(ledger, tmp_path):
    r = _recurring(ledger, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["netflix\tUSD\t15.99\t4\t2026-05-05"]


def test_recurring_min_occurrences(ledger, tmp_path):
    r = _recurring(ledger, tmp_path, "--min-occurrences", "2")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["gym\tUSD\t40.00\t2\t2026-03-11",
                                             "netflix\tUSD\t15.99\t4\t2026-05-05"]


# --- graded checks: small, independent requirements from the task text -------------------

def test_bankxml_malformed_fails(ledger, tmp_path):
    r, _ = _import(ledger, tmp_path, "bankxml", "<statement currency='EUR'><tx date=")
    assert r.returncode != 0


def test_jsonexport_bad_date_fails(ledger, tmp_path):
    doc = {"currency": "USD", "transactions": [{"booked": "2026-02-05", "value": -1, "memo": "x"}]}
    r, _ = _import(ledger, tmp_path, "jsonexport", json.dumps(doc))
    assert r.returncode != 0


def test_import_without_existing_counts(ledger, tmp_path):
    xml = ('<statement currency="GBP"><tx date="2026-03-01" amount="-1.00">A</tx>'
           '<tx date="2026-03-02" amount="-2.00">B</tx><tx date="2026-03-03" amount="-3.00">C</tx></statement>')
    r, rows = _import(ledger, tmp_path, "bankxml", xml)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 3, skipped 0 duplicates"
    assert [row[2] for row in rows[1:]] == ["GBP", "GBP", "GBP"]


def test_existing_amounts_rewritten_with_two_decimals(ledger, tmp_path):
    existing = "date,amount,currency,category,description\n2026-01-02,-30.5,EUR,food,Bakery\n"
    r, rows = _import(ledger, tmp_path, "bankxml", '<statement currency="EUR"></statement>', existing)
    assert r.returncode == 0, r.stderr
    assert rows[1] == ["2026-01-02", "-30.50", "EUR", "food", "Bakery"]


def test_same_date_imports_keep_file_order(ledger, tmp_path):
    xml = ('<statement currency="EUR"><tx date="2026-01-09" amount="-1.00">Zeta</tx>'
           '<tx date="2026-01-09" amount="-2.00">Alpha</tx><tx date="2026-01-01" amount="-3.00">First</tx></statement>')
    r, rows = _import(ledger, tmp_path, "bankxml", xml)
    assert r.returncode == 0, r.stderr
    assert [row[4] for row in rows[1:]] == ["First", "Zeta", "Alpha"]


def test_semicolon_positive_thousands(ledger, tmp_path):
    r, rows = _import(ledger, tmp_path, "semicolon", "Date;Amount;Currency;Text\n10.02.2026;1.000,00;EUR;Bonus\n")
    assert r.returncode == 0, r.stderr
    assert rows[1] == ["2026-02-10", "1000.00", "EUR", "uncategorized", "Bonus"]


def test_jsonexport_integer_value(ledger, tmp_path):
    doc = {"currency": "USD", "transactions": [{"booked": "07/03/2026", "value": -5, "memo": "Fee"}]}
    r, rows = _import(ledger, tmp_path, "jsonexport", json.dumps(doc))
    assert r.returncode == 0, r.stderr
    assert rows[1] == ["2026-03-07", "-5.00", "USD", "uncategorized", "Fee"]


def test_different_amount_is_not_duplicate(ledger, tmp_path):
    xml = '<statement currency="EUR"><tx date="2026-01-05" amount="-12.51">Lunch</tx></statement>'
    r, rows = _import(ledger, tmp_path, "bankxml", xml, EXISTING)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 1, skipped 0 duplicates" and len(rows) == 4


def test_different_currency_is_not_duplicate(ledger, tmp_path):
    xml = '<statement currency="USD"><tx date="2026-01-05" amount="-12.50">Lunch</tx></statement>'
    r, _ = _import(ledger, tmp_path, "bankxml", xml, EXISTING)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 1, skipped 0 duplicates"


def test_duplicate_needs_whitespace_normalization(ledger, tmp_path):
    xml = '<statement currency="EUR"><tx date="2026-01-02" amount="-30.00">  bakery   </tx></statement>'
    r, _ = _import(ledger, tmp_path, "bankxml", xml, EXISTING)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "imported 0, skipped 1 duplicates"


def _categorize_with(ledger, tmp_path, ledger_text, rules, *extra):
    led, rf, out = tmp_path / "l2.csv", tmp_path / "r2.json", tmp_path / "o2.csv"
    led.write_text(ledger_text)
    rf.write_text(json.dumps(rules))
    r = ledger("categorize", str(led), "--rules", str(rf), "--out", str(out), *extra)
    return r, (_rows(out.read_text()) if out.exists() else [])


def test_min_amount_is_inclusive(ledger, tmp_path):
    text = "date,amount,currency,category,description\n2026-01-01,-10.00,EUR,uncategorized,Shop\n"
    r, rows = _categorize_with(ledger, tmp_path, text, [{"category": "small", "pattern": "shop", "min_amount": -10}])
    assert r.returncode == 0, r.stderr
    assert rows[1][3] == "small"


def test_max_amount_excludes_outside(ledger, tmp_path):
    text = "date,amount,currency,category,description\n2026-01-01,-10.01,EUR,uncategorized,Shop\n"
    r, rows = _categorize_with(ledger, tmp_path, text, [{"category": "small", "pattern": "shop", "min_amount": -10}])
    assert r.returncode == 0, r.stderr
    assert rows[1][3] == "uncategorized" and r.stdout.strip() == "categorized 0"


def test_pattern_is_case_insensitive_search(ledger, tmp_path):
    text = "date,amount,currency,category,description\n2026-01-01,-3.00,EUR,uncategorized,Big COFFEE shop\n"
    r, rows = _categorize_with(ledger, tmp_path, text, [{"category": "coffee", "pattern": "coffee"}])
    assert r.returncode == 0, r.stderr
    assert rows[1][3] == "coffee"


def test_default_priority_zero(ledger, tmp_path):
    text = "date,amount,currency,category,description\n2026-01-01,-3.00,EUR,uncategorized,coffee beans\n"
    rules = [{"category": "a", "pattern": "coffee"}, {"category": "b", "pattern": "beans", "priority": 1}]
    r, rows = _categorize_with(ledger, tmp_path, text, rules)
    assert r.returncode == 0, r.stderr
    assert rows[1][3] == "b"


def test_categorize_preserves_other_columns(ledger, tmp_path):
    text = "date,amount,currency,category,description\n2026-01-01,-3.5,EUR,uncategorized,coffee\n"
    r, rows = _categorize_with(ledger, tmp_path, text, [{"category": "c", "pattern": "coffee"}])
    assert r.returncode == 0, r.stderr
    assert rows == [["date", "amount", "currency", "category", "description"],
                    ["2026-01-01", "-3.50", "EUR", "c", "coffee"]]


def _recurring_with(ledger, tmp_path, text, *extra):
    led = tmp_path / "rec.csv"
    led.write_text("date,amount,currency,category,description\n" + text)
    return ledger("recurring", str(led), *extra)


def test_gap_of_35_days_accepted(ledger, tmp_path):
    text = "2026-01-01,-10.00,USD,x,Box\n2026-02-05,-10.00,USD,x,Box\n2026-03-12,-10.00,USD,x,Box\n"
    r = _recurring_with(ledger, tmp_path, text)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["box\tUSD\t10.00\t3\t2026-04-16"]


def test_gap_of_36_days_rejected(ledger, tmp_path):
    text = "2026-01-01,-10.00,USD,x,Box\n2026-02-06,-10.00,USD,x,Box\n2026-03-13,-10.00,USD,x,Box\n"
    r = _recurring_with(ledger, tmp_path, text)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_amount_within_five_percent(ledger, tmp_path):
    ok = "2026-01-01,-100.00,USD,x,Gas\n2026-02-01,-100.00,USD,x,Gas\n2026-03-01,-104.00,USD,x,Gas\n"
    bad = "2026-01-01,-100.00,USD,x,Ice\n2026-02-01,-100.00,USD,x,Ice\n2026-03-01,-106.00,USD,x,Ice\n"
    r = _recurring_with(ledger, tmp_path, ok + bad)
    assert r.returncode == 0, r.stderr
    assert [l.split("\t")[0] for l in r.stdout.strip().splitlines()] == ["gas"]


def test_description_normalized_for_grouping(ledger, tmp_path):
    text = ("2026-01-01,-7.00,USD,x,  Cloud   Storage\n2026-01-31,-7.00,USD,x,cloud storage\n"
            "2026-03-02,-7.00,USD,x,CLOUD STORAGE \n")
    r = _recurring_with(ledger, tmp_path, text)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["cloud storage\tUSD\t7.00\t3\t2026-04-01"]


def test_next_date_uses_median_gap(ledger, tmp_path):
    text = ("2026-01-01,-5.00,USD,x,Pod\n2026-01-29,-5.00,USD,x,Pod\n"
            "2026-03-01,-5.00,USD,x,Pod\n2026-04-01,-5.00,USD,x,Pod\n")
    r = _recurring_with(ledger, tmp_path, text)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["pod\tUSD\t5.00\t4\t2026-05-02"]


def test_currency_splits_groups(ledger, tmp_path):
    text = ("2026-01-01,-5.00,USD,x,Pod\n2026-01-31,-5.00,EUR,x,Pod\n2026-03-02,-5.00,USD,x,Pod\n"
            "2026-04-01,-5.00,EUR,x,Pod\n")
    r = _recurring_with(ledger, tmp_path, text, "--min-occurrences", "2")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""
