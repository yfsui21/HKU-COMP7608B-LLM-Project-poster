"""Cache a publisher PDF for an optional studio example."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

URL="https://journals.plos.org/plosone/article/file?id=10.1371%2Fjournal.pone.0307383&type=printable"
EXPECTED="4926250510ed3abdd0bb9e9556c044ef1770de19f1247fe4c20aab6e44e5a408"

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper",choices=["hadi","forest"],default="hadi")
    forest=parser.parse_args().paper=="forest"
    url="https://aclanthology.org/2026.acl-long.15.pdf" if forest else URL
    expected="39bd4e916b4c069375acbabf3ec0cdffbf9ccfd3862738deee5540245a0dc1e5" if forest else EXPECTED
    name="posterforest-acl.pdf" if forest else "hadi-2024.pdf"
    out=Path(__file__).resolve().parents[1]/"runs"/"open-papers"/name
    out.parent.mkdir(parents=True,exist_ok=True)
    with urllib.request.urlopen(url,timeout=90) as response:
        data=response.read()
    if hashlib.sha256(data).hexdigest()!=expected:
        raise SystemExit("Publisher file differs from the checked version. Review the source PDF before importing it.")
    out.write_bytes(data)
    print(out)
