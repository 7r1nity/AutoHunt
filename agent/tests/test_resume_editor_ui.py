"""Opt-in frontend regression: all API requests are mocked; no user data is read/written.

Start the frontend, then set AUTOHUNT_RESUME_UI=1 and run this test.
"""
import os
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.skipif(
    os.getenv("AUTOHUNT_RESUME_UI") != "1", reason="Requires a running AutoHunt frontend"
)


@pytest.mark.parametrize("multiple_projects", [False, True])
def test_imported_resume_layout_edit_save_cancel(multiple_projects):
    source = "测试候选人\n\n求职意向：AI工程师\n个人优势\n编程基础\nPython、C++\n项目经历\n测试项目 2025.01—2025.06\n负责检索与评估；准确率 87%。\n未知段落也应保留 <script>\n"
    if multiple_projects:
        source = source[:source.index("项目经历")] + (
            "项目经历\n乐谱编辑器 前后端开发 2026.02-2026.05\n\n"
            "浏览器端五线谱编辑器（Capstone）核心开发\n"
            "项目概述：支持乐谱编辑、MusicXML\n导入/导出及 PDF 导出。\n"
            "技术栈：TypeScript、Puppeteer（PDF 导\n出）、Docker。\n"
            "工程质量与部署：编写测试，使\n用容器部署。\n"
            "混合式评论情感分析系统（NLP） 模型微调 + LLM 集成\n\n"
            "• 项目概述：实现情感分类，使用 Hugging Face Trans\nformers。\n"
            "•技术栈：Python、PyTorch、Ollama\n(qwen2.5:0.5b)、Gradio、scikit-learn\n"
            "模型地址 https://example.test:8080/api、路径 C:\\models\\qwen.gguf、时间 12:30\n• 模型架构：\n"
            "Ollama 本地模型部署：拉起模型。\n"
            "LangChain 链式组装：构建检索流程。\n"
            "降级容错：失败后返回分类结果。\n\n"
            "交通标志识别 — 深度学习图像分类 调优和评估\n\n"
            "简介：对比 Vision Tra\nnsformer 与 CNN\n并开展评估\n"
            "技术栈\nPyTorch、scikit-learn\n"
            "调优与消融实验\n比较消融结果；保留长段落，不把每个冒号拆为小标题。\n"
            "问题解决\n方案说明：保留自然段。实现细节：不新增字段。\n"
            "结论：准确率 89.4\n8%，保留原始数字。\n"
            "英文说明：works with\npython libraries\n"
        )
    stored = {"resume_draft": source, "profile": {
        "resume_id": "fixture", "role_suggestions": ["AI工程师"], "skills": ["Python"],
        "summary": "测试数据", "evidence": [],
    }, "preferences": {"version": 1, "locations": {"values": []}, "target_roles": {"source": "resume"},
                       "search_keywords": {"values": ["AI工程师"]}}}
    resume = {"id": "fixture", "filename": "fixture.pdf", "content_type": "application/pdf",
              "status": "parsed", "keywords": [], "role_suggestions": [], "suggested_query": "AI工程师"}
    writes = []
    errors = []

    def api(route):
        path = urlparse(route.request.url).path
        if path.endswith("/candidate-state"):
            if route.request.method == "PATCH":
                writes.append(route.request.post_data_json)
                stored["resume_draft"] = writes[-1]["content"]
            route.fulfill(json=stored)
        elif path.endswith("/resumes"):
            route.fulfill(json=[resume])
        elif path.endswith("/documents"):
            route.fulfill(json={"source_mode": "verbatim", "source_filename": "fixture.pdf"})
        elif path.endswith("/materials"):
            route.fulfill(json=[])
        elif path.endswith("/chat"):
            route.fulfill(json={"messages": []})
        else:
            route.fulfill(status=503, json={"detail": "Not part of this UI test"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="简历").click()
        panel = page.locator(".resume-draft-panel")
        preview = panel.locator(".imported-resume")
        def source_text():
            return preview.locator("[data-resume-source]").evaluate_all("nodes => nodes.map(e => e.getAttribute('data-source-content') + e.getAttribute('data-line-break')).join('')")
        expect(preview).to_be_visible()
        assert source_text() == source
        assert panel.locator("iframe").count() == 0
        assert preview.locator("h2").count() == 2
        expect(preview.locator(".resume-document-header")).to_be_visible()
        assert "gradient" in preview.locator(".resume-document-header").evaluate("e => getComputedStyle(e).backgroundImage")
        assert preview.locator(".resume-entry-card").count() > 0
        if multiple_projects:
            cards = preview.locator(".resume-entry-card")
            assert cards.count() == 3
            assert cards.nth(0).locator(".imported-resume-entry-title").inner_text().startswith("乐谱编辑器")
            assert cards.nth(1).locator(".imported-resume-entry-title").inner_text().startswith("混合式评论")
            assert cards.nth(2).locator(".imported-resume-entry-title").inner_text().startswith("交通标志识别")
            assert "项目概述" not in cards.nth(1).locator(".imported-resume-entry-title").inner_text()
            assert "Hugging Face Transformers" in cards.nth(1).inner_text()
            assert "Vision Transformer" in cards.nth(2).inner_text()
            assert "89.48%" in cards.nth(2).inner_text()
            assert "works with python libraries" in cards.nth(2).inner_text()
            stack = cards.nth(1).locator('[data-field-label="技术栈"]')
            expect(stack).to_contain_text("(qwen2.5:0.5b)、Gradio、scikit-learn")
            expect(stack).to_contain_text("https://example.test:8080/api")
            expect(stack).to_contain_text("C:\\models\\qwen.gguf")
            expect(stack).to_contain_text("12:30")
            assert cards.nth(1).locator("[data-field-label]").evaluate_all("nodes => nodes.map(e => e.dataset.fieldLabel)") == ["项目概述", "技术栈", "模型架构"]
            expect(cards.nth(1).locator('[data-field-label="模型架构"]')).to_contain_text("降级容错：失败后返回分类结果。")
            expect(cards.nth(2).locator('[data-field-label="技术栈"]')).not_to_contain_text("调优与消融实验")
            expect(cards.nth(2).locator('[data-field-label="调优与消融实验"]')).to_contain_text("比较消融结果")
            expect(cards.nth(2).locator('[data-field-label="问题解决"]')).to_contain_text("实现细节：不新增字段。")
            # Every non-whitespace source character must also remain visible.
            visible = "".join(preview.locator("[data-resume-source]").all_text_contents())
            assert "".join(visible.split()) == "".join(source.split())
        assert preview.locator(".resume-skill-matrix").count() == 1
        assert preview.locator(".resume-section-heading > span").all_text_contents() == ["01", "02"]
        panel.get_by_role("button", name="编辑", exact=True).click()
        editor = panel.get_by_role("group", name="排版简历编辑器")
        expect(editor).to_be_visible()
        expect(editor.locator(".resume-document-header")).to_be_visible()
        assert editor.locator(".resume-entry-card").count() > 0
        if multiple_projects:
            assert editor.locator(".resume-entry-card").count() == 3
            assert "导出" in editor.locator(".imported-resume-detail").nth(1).input_value()
        field = editor.get_by_role("textbox", name="简历第 6 段", exact=True)
        expect(field).to_have_value("Python、C++")
        field.fill("Python、C++、PyTorch\n补充一行")
        panel.get_by_role("button", name="取消", exact=True).click()
        assert source_text() == source
        assert writes == []
        panel.get_by_role("button", name="编辑", exact=True).click()
        field.fill("Python、C++、PyTorch\n补充一行")
        if multiple_projects:
            editor.locator(".imported-resume-detail").nth(1).fill("技术栈：TypeScript、Puppeteer（PDF 导出）、Docker、Jest。")
        page.set_viewport_size({"width": 390, "height": 844})
        assert editor.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        assert field.evaluate("e => e.scrollHeight <= e.clientHeight + 2")
        panel.get_by_role("button", name="保存修改", exact=True).click()
        expect(editor).not_to_be_visible()
        expected = source.replace("Python、C++", "Python、C++、PyTorch\n补充一行")
        if multiple_projects:
            expected = expected.replace("技术栈：TypeScript、Puppeteer（PDF 导\n出）、Docker。", "技术栈：TypeScript、Puppeteer（PDF 导出）、Docker、Jest。")
        assert writes == [{"content": expected}]
        assert source_text() == expected
        page.reload()
        page.get_by_role("button", name="简历").click()
        expect(preview).to_be_visible()
        assert source_text() == expected
        if os.getenv("AUTOHUNT_RESUME_SCREENSHOT"):
            page.set_viewport_size({"width": 1440, "height": 1100})
            preview.screenshot(path=os.environ["AUTOHUNT_RESUME_SCREENSHOT"].replace(".png", f"-{multiple_projects}.png"), style=".topbar { visibility: hidden !important; }")
        assert not errors
        browser.close()


@pytest.mark.parametrize("saved_layout", [False, True])
def test_pdf_sections_skills_and_markdown_supplement(saved_layout):
    source = (
        "排版测试候选人\nAI / LLM 工程师\n"
        "L · L · M · · · · · R · A · G · · · · · P · o · s · t · - · t · r · a\n"
        " · i · n · i · n · g · · · · · P · y · T · o · r · c · h\n"
        "职业摘要\n· 具备模型开发经验，覆盖检索与\n 服务部署。\n"
        "核心技能\nLlama/Qwen/DeepSeek、Transformers、\n"
        "LLM / NLP SFT、DPO、Function\nCalling\n"
        "LangGraph、Hybrid Search、\nRAG / Agent Reranker、MCP/Tool\nCalling\n"
        "训练与评测 PyTorch、Weights & Biases、lm-eval-\nharness\n"
        "数据工程 SQL、数据质\n量采样\n"
        "工作经历\n示例实验室 | 工程师 2021.07 - 2022.06\n南京\n"
        "· 搭建文本分类模型。\n· 字段准确率从 93.1% 提升至 97.6%。\n"
        "代表项目\n知识助手 | Qwen2.5 / LangGraph / FastAPI\n"
        "· 接入 170 万 chunk。\n· Faithfulness 从 0.76 提升至 0.89。\n"
        "研究助理 | Planner / Python Sandbox\n· 成功率从 71% 提升至 88%。\n"
        "教育背景\n示例大学 硕士 | 2019 - 2021\n其他\n· 阅读英文论文。\n"
        "\n## 已确认的补充经历\n## 项目经历\n### 金融知识检索系统\n"
        "- 项目简介：面向金融研究的检索问答系统。\n"
        "- 技术栈：Python、Ollama (qwen2.5:0.5b)、FastAPI\n"
        "- 项目特点：实现 BM25 与向量检索。\n- 项目特点：提供引用溯源。\n"
        "- 调优：缓存后 P50 从 1.42s 降至 0.31s。\n- 调优：使用 FP16。\n"
    )
    state = {"resume_draft": source, "profile": {"resume_id": "fixture", "role_suggestions": [], "skills": [], "summary": "测试", "evidence": []},
             "preferences": {"version": 1, "locations": {"values": []}, "target_roles": {"source": "resume"}, "search_keywords": {"values": []}}}
    document = {"source_mode": "verbatim", "source_filename": "fixture.pdf"}
    if saved_layout:
        from app.resume_layout import source_blocks
        blocks = source_blocks(source)
        # Deliberately coarse old plan must not swallow explicit source headings.
        document["layout"] = {"status": "validated", "checks": [], "issues": [], "blocks": [b.model_dump() for b in blocks],
                              "nodes": [{"role": "paragraph", "label": "", "refs": [b.id for b in blocks]}]}
    writes = []

    def api(route):
        path = urlparse(route.request.url).path
        if path.endswith("/candidate-state"):
            if route.request.method == "PATCH":
                writes.append(route.request.post_data_json)
            route.fulfill(json=state)
        elif path.endswith("/documents"):
            route.fulfill(json=document)
        elif path.endswith(("/resumes", "/materials")):
            route.fulfill(json=[])
        elif path.endswith("/chat"):
            route.fulfill(json={"messages": []})
        else:
            route.fulfill(status=503, json={"detail": "Isolated fixture"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/api/agent/**", api)
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="简历").click()
        panel = page.locator(".resume-draft-panel")
        preview = panel.locator(".imported-resume")
        expect(preview).to_be_visible()
        assert preview.locator("[data-resume-source]").evaluate_all("nodes => nodes.map(e => e.dataset.sourceContent + e.dataset.lineBreak).join('')") == source
        expect(preview.locator(".imported-resume-keywords")).to_have_text("LLM · RAG · Post-training · PyTorch")
        sections = preview.locator(".resume-section")
        projects = sections.filter(has=page.get_by_role("heading", name="代表项目", exact=True))
        assert projects.locator(".resume-entry-card").count() == 2
        work = sections.filter(has=page.get_by_role("heading", name="工作经历", exact=True))
        expect(work).not_to_contain_text("知识助手")
        assert work.locator(".imported-resume-bullet").count() == 2
        skill_rows = preview.locator(".imported-resume-skill")
        assert skill_rows.count() == 4
        expect(skill_rows.nth(0)).to_contain_text("Function Calling")
        expect(skill_rows.nth(1)).to_contain_text("LangGraph")
        expect(skill_rows.nth(2)).to_contain_text("lm-eval-harness")
        expect(skill_rows.nth(3)).to_contain_text("质量采样")
        expect(preview.locator(".imported-detail-group h4")).to_have_text(["项目特点", "调优"])
        assert preview.locator(".imported-detail-group li").count() == 4
        expect(preview).to_contain_text("qwen2.5:0.5b")
        expect(preview).to_contain_text("0.31s")
        # A save without edits must preserve every original character.
        panel.get_by_role("button", name="编辑", exact=True).click()
        panel.get_by_role("button", name="保存修改", exact=True).click()
        expect(panel.get_by_role("button", name="编辑", exact=True)).to_be_visible()
        assert writes == [{"content": source}]
        if os.getenv("AUTOHUNT_RESUME_SCREENSHOT"):
            preview.screenshot(path=os.environ["AUTOHUNT_RESUME_SCREENSHOT"].replace(".png", f"-refined-{saved_layout}.png"), style=".topbar { visibility: hidden !important; }")
        page.set_viewport_size({"width": 390, "height": 844})
        assert preview.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        browser.close()
