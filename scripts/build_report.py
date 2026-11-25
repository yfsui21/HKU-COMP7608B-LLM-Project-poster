"""Compile the report with Tectonic and record which source produced the PDF."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tectonic', default=shutil.which('tectonic'))
    args = parser.parse_args()
    if not args.tectonic:
        parser.error('Install Tectonic or pass --tectonic /path/to/tectonic')
    root = Path(__file__).resolve().parents[1]
    report = root / 'report'
    with tempfile.TemporaryDirectory() as build:
        subprocess.run([args.tectonic, '-X', 'compile', str(report / 'main.tex'),
                        '--outdir', build, '--keep-logs'], check=True, cwd=root)
        pdf = Path(build) / 'main.pdf'
        if not pdf.read_bytes().startswith(b'%PDF'):
            raise ValueError('Compiler did not produce a PDF')
        shutil.copyfile(pdf, report / 'main.pdf')
        log = (Path(build) / 'main.log').read_text(errors='replace')
    sources = [report / 'main.tex', report / 'neurips_2026.sty', *sorted((report / 'figs').glob('*'))]
    record = {
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'compiler': subprocess.check_output([args.tectonic, '--version'], text=True).strip(),
        'sources': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sources if p.is_file()},
        'pdf_sha256': hashlib.sha256((report / 'main.pdf').read_bytes()).hexdigest(),
        'overfull_box_warnings': sum('Overfull' in line for line in log.splitlines()),
    }
    (report / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    print(report / 'main.pdf')


if __name__ == '__main__':
    main()
