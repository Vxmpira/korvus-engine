"""Offline tests for korvus_edgar parsers. Fixtures are trimmed copies of real
EDGAR responses captured 2026-11-09 (getcurrent feed) and NVIDIA's Q2 FY27
earnings 8-K (accession 0001045810-26-000073, accepted 2026-08-26 16:21:19 ET).
Run:  python -m pytest tests/test_edgar.py -q   (or just: python tests/test_edgar.py)
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_edgar as ke

FEED = '''<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings - Mon, 09 Nov 2026 13:01:17 EST</title>
<entry>
<title>8-K - MUELLER INDUSTRIES INC (0000089439) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/89439/000008943926000044/0000089439-26-000044-index.htm"/>
<summary type="html">
 &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;b&gt;AccNo:&lt;/b&gt; 0000089439-26-000044 &lt;b&gt;Size:&lt;/b&gt; 328 KB
&lt;br&gt;Item 5.02: Departure of Directors or Certain Officers; Election of Directors
&lt;br&gt;Item 8.01: Other Events
&lt;br&gt;Item 9.01: Financial Statements and Exhibits
</summary>
<updated>2026-11-09T12:37:10-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0000089439-26-000044</id>
</entry>
<entry>
<title>8-K - NVIDIA CORP (0001045810) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/0001045810-26-000073-index.htm"/>
<summary type="html">
 &lt;b&gt;Filed:&lt;/b&gt; 2026-08-26 &lt;b&gt;AccNo:&lt;/b&gt; 0001045810-26-000073 &lt;b&gt;Size:&lt;/b&gt; 802 KB
&lt;br&gt;Item 2.02: Results of Operations and Financial Condition
&lt;br&gt;Item 9.01: Financial Statements and Exhibits
</summary>
<updated>2026-08-26T16:21:19-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0001045810-26-000073</id>
</entry>
<entry>
<title>8-K - GREENE BANCORP, INC. (0001070524) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1070524/000114036126042702/0001140361-26-042702-index.htm"/>
<summary type="html">
 &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;b&gt;AccNo:&lt;/b&gt; 0001140361-26-042702 &lt;b&gt;Size:&lt;/b&gt; 342 KB
&lt;br&gt;Item 5.03: Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year
&lt;br&gt;Item 9.01: Financial Statements and Exhibits
</summary>
<updated>2026-11-09T12:17:05-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001140361-26-042702</id>
</entry>
</feed>'''

INDEX = '''<div class="formGrouping"><div class="infoHead">Accepted</div>
<div class="info">2026-08-26 16:21:19</div></div>
<table class="tableFile" summary="Document Format Files">
 <tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Size</th></tr>
 <tr><td scope="row">1</td><td scope="row">8-K</td>
  <td scope="row"><a href="/ix?doc=/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm">nvda-20260826.htm</a> &nbsp;&nbsp;<span style="color: green">iXBRL</span></td>
  <td scope="row">8-K</td><td scope="row">26457</td></tr>
 <tr class="evenRow"><td scope="row">2</td><td scope="row">EX-99.1</td>
  <td scope="row"><a href="/Archives/edgar/data/1045810/000104581026000073/q2fy27pr.htm">q2fy27pr.htm</a></td>
  <td scope="row">EX-99.1</td><td scope="row">341113</td></tr>
 <tr><td scope="row">3</td><td scope="row">EX-99.2</td>
  <td scope="row"><a href="/Archives/edgar/data/1045810/000104581026000073/q2fy27cfocommentary.htm">q2fy27cfocommentary.htm</a></td>
  <td scope="row">EX-99.2</td><td scope="row">256881</td></tr>
 <tr><td scope="row">7</td><td scope="row">NVDA LOGO</td>
  <td scope="row"><a href="/Archives/edgar/data/1045810/000104581026000073/nvdalogoa19.jpg">nvdalogoa19.jpg</a></td>
  <td scope="row">GRAPHIC</td><td scope="row">13441</td></tr>
</table>'''

EX991 = '''<DOCUMENT>
<TYPE>EX-99.1
<SEQUENCE>2
<FILENAME>q2fy27pr.htm
<DESCRIPTION>EX-99.1
<TEXT>
<html><head><title>Document</title><script >bazadebezolkohpepadr="1842860671"</script></head><body>
<div style="margin-bottom:15pt"><font style="font-weight:700">NVIDIA Announces Financial Results for Second Quarter Fiscal 2027</font></div>
<div><font>&#8226;</font><font>Revenue of $96.2 billion, up 106% from a year ago</font></div>
<div><font>&#8226;</font><font>Data Center revenue of $89.0 billion, up 117% from a year ago</font></div>
<table><tr><td>Revenue</td><td>$</td><td>96,221</td><td>$</td><td>81,615</td></tr></table>
</body></html>'''


def test_feed():
    rows = ke.parse_feed(FEED)
    assert len(rows) == 3
    nv = rows[1]
    assert nv["cik"] == 1045810 and nv["form"] == "8-K"
    assert nv["accession"] == "0001045810-26-000073"
    assert nv["items"] == ["2.02", "9.01"]
    assert nv["url"].endswith("0001045810-26-000073-index.htm")
    assert ke.to_utc_iso(nv["updated"]).startswith("2026-08-26T20:21:19")


def test_filters():
    rows = ke.parse_feed(FEED)
    assert ke.worth_it(rows[0]) is True     # 5.02 + 8.01
    assert ke.worth_it(rows[1]) is True     # earnings
    assert ke.worth_it(rows[2]) is False    # bylaws + exhibits only
    assert ke.worth_it({"items": []}) is True   # 6-K carries no item codes
    assert ke.describe_items(["9.01", "2.02"]) == "earnings release"


def test_index_and_pick():
    idx = ke.parse_index(INDEX)
    assert idx["accepted"] == "2026-08-26 16:21:19"
    assert ke.accepted_et_to_utc(idx["accepted"]).startswith("2026-08-26T20:21:19")
    types = [d["type"] for d in idx["docs"]]
    assert types == ["8-K", "EX-99.1", "EX-99.2", "GRAPHIC"]
    assert idx["docs"][0]["url"] == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm"
    doc = ke.pick_document(idx["docs"], "8-K")
    assert doc["type"] == "EX-99.1" and doc["url"].endswith("q2fy27pr.htm")
    # no exhibit -> falls back to the 8-K body
    body_only = [d for d in idx["docs"] if not d["type"].startswith("EX")]
    assert ke.pick_document(body_only, "8-K")["type"] == "8-K"


def test_text_and_headline():
    txt = ke.html_to_text(EX991)
    assert "bazadebezol" not in txt and "<TYPE>" not in txt and "Document" not in txt.split("\n")[0]
    assert txt.split("\n")[0] == "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
    assert "Revenue of $96.2 billion, up 106% from a year ago" in txt
    assert "Revenue | $ | 96,221 | $ | 81,615" in txt
    h = ke.pick_headline(txt, "NVDA", "NVIDIA CORP", ["2.02"], "8-K")
    assert h == "NVDA: NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
    assert ke.pick_headline("", "AAPL", "Apple", ["5.02"], "8-K") == "AAPL files 8-K: executive / board change"


def test_watchlist_base():
    assert ke.BASE_WATCHLIST["NVDA"] == 1045810
    assert ke.BASE_WATCHLIST["XOM"] == 2115436


def test_tidy_title():
    import korvus_engine as eng
    T = eng.tidy_title
    assert T("COST: $30 Special Dividend (~$13.3B), $4B Repurchase Reauthorized Through 2030") == \
        "$30 Special Dividend, $4B Repurchase Reauthorized Through 2030"
    assert T("TSMC Q3 2026: Revenue NT$1.49T, EPS NT$29.43; Board Approves US$40.9B 2027 Capex") == \
        "TSMC Q3 2026: Revenue NT$1.49T, EPS NT$29.43"
    assert T("Apple raises $7.5B in a new bond sale") == "Apple raises $7.5B in a new bond sale"      # clean titles pass through
    assert T("BRK-B: Berkshire adds to its Japan stakes") == "Berkshire adds to its Japan stakes"
    long = T("Costco declares a $30 special dividend and renews its buyback program through the end of the decade, as fees climb")
    assert len(long) <= 84 and not long.lower().endswith((" the", " and", " its", " of")), long
    assert T("") == "" and T(None) == ""
    assert "at most ONE number" in eng.SYSTEM_PROMPT and "NT$1.49T, EPS" in eng.SYSTEM_PROMPT   # the bad example is shown as bad
    assert eng._structured({"title": "COST: Special dividend declared; more"}, lambda x: x)["title"] == "Special dividend declared"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
