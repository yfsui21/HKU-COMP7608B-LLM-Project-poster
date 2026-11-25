"""Run pinned PosterForest from a fresh PDF using GPT text and vision inference."""
import argparse
import ast
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

REV = "dd4b060f3e5e8ed511693bdc9e24a1d1e57165f9"
ROOT = Path(__file__).resolve().parents[1]


def prepare_runtime(source, work):
    """Assemble a portable copy. Apply I/O adaptations to the pinned source only."""
    def git(*args):
        return subprocess.check_output(["git", "-c", "safe.directory=" + source.as_posix(),
                                       "-C", str(source), *args])
    if git("rev-parse", "HEAD").decode().strip() != REV:
        raise ValueError("Checkout the documented PosterForest revision first.")

    def original(name):
        return git("show", REV + ":" + name).decode("utf-8")

    def save(name, content):
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Third-party code and templates are read from git objects, not edited in the clone.
    names = git("ls-tree", "-r", "--name-only", REV, "utils/prompts", "utils/prompt_templates").decode().splitlines()
    for name in names:
        if name.endswith((".txt", ".yaml", ".json", ".j2")):
            save(name, original(name))
    for name in ["utils/pptx_utils.py", "utils/critic_utils.py", "PosterForest/poster_config.py",
                 "PosterForest/tree_visualization.py", "PosterForest/step_02b_asset_management.py",
                 "PosterForest/step_03_poster_layout.py", "PosterForest/step_04_layout_hierarchy.py"]:
        save(name, original(name))
    for name in ["PosterForest/__init__.py", "utils/__init__.py", "utils/src/__init__.py"]:
        save(name, "")
    save("utils/src/utils.py", "from runtime import get_json_from_response, ppt_to_images, ppt_to_images_with_title\n")
    # Retain the upstream helpers reached by this non-ablation path; its other
    # utilities import cloud providers, CUDA models and browser automation.
    name = "utils/wei_utils.py"
    code = original(name)
    keep = {"account_token", "run_code", "style_bullet_content", "generate_length_suggestions",
            "scale_to_target_area", "char_capacity"}
    functions = [ast.get_source_segment(code, n) for n in ast.parse(code).body
                 if isinstance(n, ast.FunctionDef) and n.name in keep]
    save(name, "import io, contextlib, traceback, math, os, re, copy, json\nfrom pathlib import Path\n"
         "from utils.pptx_utils import *\nfrom utils.critic_utils import *\n"
         "from runtime import get_agent_config, make_model, agent_step_with_timeout\n\n" + "\n\n".join(functions))

    name = "PosterForest/step_01a_document_parsing.py"
    code = original(name)
    prefix = "import json, os, re\nfrom pathlib import Path\nfrom jinja2 import Template\nfrom tenacity import retry, stop_after_attempt\nfrom runtime import ChatAgent, parse_pdf_input, get_json_from_response\nfrom utils.wei_utils import make_model, agent_step_with_timeout, account_token\n"
    code = prefix + code[code.index("def _cut_text_by_regex"):]
    code = code[:code.index("pipeline_options =")] + code[code.index("def _correct_asset_assignments"):]
    start = code.index("    # ---- 1-1: parse PDF")
    stop = code.index("    figures_info = {}", start)
    code = code[:start] + "    body_text, figures, tables = parse_pdf_input(args, step_dir)\n    docling_result = None\n\n" + code[stop:]
    code = code.replace("    full_body_text = body_text", "    from runtime import fit_document\n    full_body_text = body_text\n    body_text = fit_document(body_text)")
    code = code.replace("    if not has_title:", "    from runtime import restore_title_section\n    if not has_title and not restore_title_section(sections_json, body_text):")
    save(name, code)

    for filename in ["step_02a_tree_refinement", "step_05_content_generation", "step_06_modification_planning"]:
        name = "PosterForest/" + filename + ".py"
        code = original(name).replace("from camel.agents import ChatAgent", "from runtime import ChatAgent")
        code = code.replace("from camel.messages import BaseMessage", "from runtime import BaseMessage")
        save(name, code)

    name = "PosterForest/step_07_pptx_generation.py"
    code = original(name)
    # Python literals in emitted code must escape Windows path separators and quotes.
    code = code.replace('file_name="{output_file}"', 'file_name={output_file!r}').replace(
        'image_path="{abs_img_path}"', 'image_path={abs_img_path!r}').replace("open('{tmp_name}', 'r')", "open({tmp_name!r}, 'r')")
    code = code.replace("{raw_name}\n", "{' '.join(str(raw_name).splitlines())}\n")
    save(name, code)

    name = "PosterForest/main.py"
    code = original(name)
    code = ("from PosterForest.step_01a_document_parsing import parse_hierarchical_document\n"
            "from PosterForest.step_02a_tree_refinement import refine_document_tree\n"
            "from PosterForest.step_03_poster_layout import generate_panel_layout, count_total_nodes\n"
            "from PosterForest.tree_visualization import save_tree_with_metadata, print_tree_unified\n"
            + code[code.index("from utils.wei_utils import"):])
    code = re.sub(r"^from utils\.ablation_utils import .*\n", "", code, flags=re.M)
    save(name, code)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pdf", type=Path)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--backend", choices=["codex", "session", "api"], default="codex")
    p.add_argument("--model", default="codex-default")
    p.add_argument("--soffice", default=shutil.which("soffice"))
    args = p.parse_args()
    pdf, work, source = args.pdf.resolve(strict=True), args.out.resolve(), args.source.resolve(strict=True)
    if not args.soffice:
        p.error("--soffice or LibreOffice on PATH is required")
    work.mkdir(exist_ok=True, parents=True)
    identity = dict(pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), source=REV, model=args.model, backend=args.backend)
    marker = work / "input.json"
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise ValueError("Use a fresh output directory for a different PDF or model.")
    if not marker.exists() and any(work.iterdir()):
        raise ValueError("Use a fresh output directory.")
    marker.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    prepare_runtime(source, work)
    os.environ.update(FOREST_MODEL=args.model, FOREST_BACKEND=args.backend,
                      FOREST_SOFFICE=str(Path(args.soffice).resolve()), MPLCONFIGDIR=str(work / "mpl"))
    sys.path[:0] = [str(work), str(ROOT / "examples/forest_local"), str(ROOT)]
    os.chdir(work)
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", pdf.stem)[:48] or "paper"
    sys.argv = ["PosterForest.main", "--paper_path", pdf.as_posix(), "--poster_name", slug,
                "--model_name_t", "gpt", "--model_name_v", "gpt",
                "--poster_width_inches", "48", "--poster_height_inches", "36"]
    from audit import finish
    outputs = work / "outputs"
    previous = set(outputs.iterdir()) if outputs.exists() else set()
    trace = work / "inference.jsonl"
    trace_start = len(trace.read_text(encoding="utf-8").splitlines()) if trace.exists() else 0
    runpy.run_module("PosterForest.main", run_name="__main__")
    created, = set(outputs.iterdir()) - previous
    finish(work, created, pdf, slug, trace_start)


if __name__ == "__main__":
    main()
