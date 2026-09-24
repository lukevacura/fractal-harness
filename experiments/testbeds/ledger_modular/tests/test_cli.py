from ledger.cli import main


def test_summary(tmp_path, capsys):
    p = tmp_path / "l.csv"
    p.write_text("date,amount,currency,category,description\n2026-01-02,-3.50,EUR,food,x\n")
    assert main(["summary", str(p)]) == 0
    assert capsys.readouterr().out == "food\t3.50\n"
