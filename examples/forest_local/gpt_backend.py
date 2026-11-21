"""Use Codex CLI for unattended GPT inference or exchange requests with the active GPT session."""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def complete(payload, key):
    root = Path("gpt-requests") / key
    root.mkdir(parents=True, exist_ok=True)
    prompt, images = [], []
    for msg in payload["messages"]:
        parts = msg["content"]
        if isinstance(parts, str):
            prompt.append(msg["role"] + "\n" + parts)
            continue
        for part in parts:
            if part["type"] == "text":
                prompt.append(msg["role"] + "\n" + part["text"])
            else:
                image = root / f"image-{len(images)}.jpg"
                image.write_bytes(base64.b64decode(part["image_url"]["url"].split(",", 1)[1], validate=True))
                images.append(image.resolve())
    prompt = "\n\n".join(prompt)
    (root / "prompt.txt").write_text(prompt, encoding="utf-8")
    model = payload["model"]
    if os.environ["FOREST_BACKEND"] == "session":
        # No desktop login token or API key is exposed to this process.
        pending = Path("pending.json")
        tmp = pending.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(key=key, images=len(images))), encoding="utf-8")
        tmp.replace(pending)
        answer = root / "answer.json"
        deadline = time.monotonic() + 1800
        while not answer.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"No active-session response to {key}")
            time.sleep(.3)
        content = json.dumps(json.loads(answer.read_text(encoding="utf-8")), ensure_ascii=False)
        pending.unlink(missing_ok=True)
        # Upstream sums integers. The audit explicitly reports usage as unavailable.
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reported": False}
    else:
        exe = shutil.which("codex")
        if not exe:
            raise RuntimeError("Install and sign in to Codex CLI or use the active session backend.")
        with tempfile.TemporaryDirectory() as temp:
            out = (root / "last-message.txt").resolve()
            cmd = [exe, "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--json", "-C", temp, "-o", str(out)]
            if model != "codex-default":
                cmd.extend(["-m", model])
            for image in images:
                cmd.extend(["-i", str(image)])
            cmd.extend(["--", "-"])
            task = "Answer the enclosed poster-generation request as a text/vision model. Use only the supplied data and attached images. Use no tools. Return the requested JSON.\n\n" + prompt
            proc = subprocess.run(cmd, input=task, text=True, encoding="utf-8", errors="replace",
                                  capture_output=True, timeout=1200)
            if proc.returncode:
                raise RuntimeError("Codex inference failed: " + proc.stderr[-1200:])
            content = out.read_text(encoding="utf-8")
            events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
            stats = next((e["usage"] for e in reversed(events) if e.get("type") == "turn.completed"), {})
            usage = dict(prompt_tokens=stats.get("input_tokens", 0), completion_tokens=stats.get("output_tokens", 0),
                         reported=bool(stats))
    return {"choices": [{"message": {"content": content}}], "usage": usage}
