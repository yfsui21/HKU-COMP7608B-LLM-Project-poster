"""Reviewed evidence outline for the ACL publisher PDF; page coordinates are in PDF points."""
import hashlib
from pathlib import Path

DIGEST="39bd4e916b4c069375acbabf3ec0cdffbf9ccfd3862738deee5540245a0dc1e5"
PATH=Path("runs/open-papers/posterforest-acl.pdf")

def matches(path):
    return path.stat().st_size==50538737 and hashlib.sha256(path.read_bytes()).hexdigest()==DIGEST

def figure(page,caption,coords):
    l,t,r,b=coords
    return dict(page=page,caption=caption,box=[l/595.276,t/841.89,r/595.276,b/841.89])

def plan():
    main=figure(3,"Figure 2: Overview of PosterForest.",(69,69,526,249))
    traversal=figure(5,"Figure 3: Poster Tree Traversal (Node-level).",(70,69,291,156))
    traversal.update(claim="Figure 3: Poster Tree Traversal (Node-level).",claim_page=5)
    problem=figure(1,"Figure 1: Limitations of Current SPG Methods.",(308,219,523,315))
    comparison=figure(7,"Figure 4: Qualitative Comparison.",(70,70,525,155))
    ablation=figure(8,"Figure 5: Effect of Content and Layout Agents.",(70,70,526,236))
    sections=[
        dict(label="Research question",page=1,quote="Automating scientific poster generation requires hierarchical document understanding and coherent content-layout planning.",
             text="How can scientific poster generation preserve a paper’s argument when text and figures compete for limited space?",required=["poster generation"]),
        dict(label="Method",page=3,quote="PosterForest first constructs a hierarchical Poster Tree that integrates document semantics and layout (Section 3.2), then iteratively refines it through collaboration between agents to optimize structure and visual coherence (Section 3.3).",
             text="The Poster Tree links sections, subsections, text and visual assets to nested regions on the canvas. Content and layout agents revise the same structure.",required=["Poster Tree"]),
        dict(label="Human preference",page=6,quote="Ours 56.0 % 53.2 % 59.6 % 59.2 %",
             text="PosterForest received 59.2 % of overall first choices in the authors’ comparison of four generated posters.",required=["59.2 %"])
    ]
    tree=dict(authors="Jiho Choi  ·  Seojeong Park  ·  Seongjong Song  ·  Hyunjung Shim",
        affiliation="KAIST  /  Yonsei University                                      ACL 2026",
        credit="Adapted from Choi, Park, Song & Shim, ACL 2026, pp. 379–401. Figures 1–5 and Table 2 from the paper. DOI: 10.18653/v1/2026.acl-long.15.",
        takeaway="Link each figure to its scientific claim before allocating space. Revisit both the claim and its region when a local edit affects nearby panels.",takeaway_page=5,
        limit="Dense clusters of figures and tables can break extraction or matching. An omitted or mismatched image can propagate through the entire generation process.",limit_page=13,
        editorial="Source-checked demonstration for a same-paper visual comparison. Figure 4 is shown as its first row. Human preference percentages are selected from Table 2.",
        columns=[
            dict(title="Problem and study design",panels=[
                dict(title="From paper to poster",section=0,page=1),
                dict(title="What gets lost in a flat summary?",asset="exhibit-0",page=1,
                     text="The paper illustrates two earlier systems. Preserving a section heading alone does not preserve the relationship between a finding and its visual evidence."),
                dict(title="Evaluation design",steps=[
                    ["Benchmark","The authors use 100 Paper2Poster paper–poster pairs for quantitative evaluation."],
                    ["Human study","25 graduate participants compared four generated alternatives for each of ten papers. Fonts and colors were standardized."]],page=7)
            ]),
            dict(title="Hierarchical generation",panels=[
                dict(title="A shared representation",section=1,asset="figure",page=3),
                dict(title="Node-level revision",asset="support",text="A content agent edits the amount of text. A layout agent reallocates regions and checks alignment. Changes at a parent node constrain its children.",page=5),
                dict(title="Whole-poster review",text="After traversal, a feedback agent reviews the rendered poster and decides whether another tree-level pass is needed.",page=6)
            ]),
            dict(title="Comparisons and ablations",panels=[
                dict(title="Qualitative comparison",asset="exhibit-1",text="One example from Figure 4. Left to right: author poster, P2P, Paper2Poster and PosterForest.",page=7),
                dict(title="Human first choices",section=2,table=[["Method","Structure","Overall"],["4o-HTML","2.4%","1.6%"],["P2P","13.2%","12.0%"],["Paper2Poster","24.8%","27.2%"],["PosterForest","59.6%","59.2%"]],page=6),
                dict(title="What each agent contributes",asset="exhibit-2",text="Figure 5 compares the base, content-only, layout-only and joint configurations. The annotations identify imbalance and redundant content.",page=8)
            ])])
    return dict(title="PosterForest: Hierarchical Multi-Agent Collaboration for Scientific Poster Generation",sections=sections,
        evidence=main,secondary=traversal,exhibits=[problem,comparison,ablation],conference=tree,credit=tree["credit"],planner="reviewed",source_label="ACL 2026")
