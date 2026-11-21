"""Cache the publisher's open-access PDF for the optional Hadi et al. example."""
import hashlib
from pathlib import Path
import urllib.request

URL="https://journals.plos.org/plosone/article/file?id=10.1371%2Fjournal.pone.0307383&type=printable"
EXPECTED="4926250510ed3abdd0bb9e9556c044ef1770de19f1247fe4c20aab6e44e5a408"

if __name__=="__main__":
    out=Path(__file__).resolve().parents[1]/"runs"/"open-papers"/"hadi-2024.pdf"
    out.parent.mkdir(parents=True,exist_ok=True)
    with urllib.request.urlopen(URL,timeout=45) as response:
        data=response.read()
    if hashlib.sha256(data).hexdigest()!=EXPECTED:
        raise SystemExit("Publisher file differs from the checked version. Review the source PDF before importing it.")
    out.write_bytes(data)
    print(out)
