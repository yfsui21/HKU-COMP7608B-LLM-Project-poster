"""Run and record a real model-assisted native-tree edit, including rejected attempts."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_canvas.agent import read, write
from paper_canvas.model import Model
from paper_canvas.native_revision import revise


class RecordedModel(Model):
    def __init__(self):
        super().__init__(True)
        self.responses = []

    def ask(self, prompt, data, image=None):
        answer = super().ask(prompt, data, image)
        self.responses.append(dict(stage=prompt.split('\n')[0], response=answer))
        return answer


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('session', type=Path)
    p.add_argument('request')
    p.add_argument('--soffice', required=True)
    p.add_argument('--record', type=Path, required=True)
    args = p.parse_args()
    api = RecordedModel()
    before = read(args.session / 'state.json')
    result = dict(request=args.request, model=api.name, started_at_utc=datetime.now(timezone.utc).isoformat(),
                  before_artifact=before['artifact'], accepted=False)
    start = time.monotonic()
    try:
        after = revise(args.session, args.request, api, args.soffice, expected_version=before['artifact'])
        result.update(accepted=True, after_artifact=after['artifact'], operations=after['history'][-1])
    except Exception as exc:
        result.update(error=str(exc), after_artifact=read(args.session / 'state.json')['artifact'])
    finally:
        result.update(elapsed_seconds=round(time.monotonic()-start, 2), calls=api.calls,
                      responses=api.responses)
        args.record.parent.mkdir(parents=True, exist_ok=True)
        write(args.record, result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('calls','operations')}, indent=2))
    if not result['accepted']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
