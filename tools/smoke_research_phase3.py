from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.research import ResearchAdapter
from agent_host.provider_catalog import BROWSER_MANIFEST, OPENCLAW_MANIFEST, RESEARCH_MANIFEST
from agent_host.provider_contract import select_provider
from llm.prompts import render_provider_routing_addon
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from research.engine import ResearchEngine
from research.fetcher import FetchResult, extract_html_text
from research.llm import DeepSeekResearchLLM
from research.models import ResearchAnalysis, ResearchConflict, ResearchFinding, ResearchPlan, SearchResult
from research.ranker import SourceRanker
from research.search import SearchBackend, SearchRouter, parse_duckduckgo_html
from server.provider_requirements import DelegateRequirementFacts, compile_delegate_requirements


class EmptyBackend(SearchBackend):
    name = "empty"

    async def search(self, query: str, *, limit: int):
        return []


class WorkingBackend(SearchBackend):
    name = "working"

    async def search(self, query: str, *, limit: int):
        return [SearchResult(title="Found", url="https://example.com/found", provider=self.name, query=query, rank=1)]


class SlowEngine:
    def classify(self, task: str) -> str:
        return "deep"

    async def run(self, task: str, *, mode=None, progress=None):
        if progress is not None:
            await progress("planning", {"mode": "deep"})
        await asyncio.sleep(0.08)
        from research.models import ResearchResult
        return ResearchResult(task=task, mode="deep", text="# 研究报告：slow", metadata={})


class FakeSearchRouter:
    last_backend = "fake"

    async def search(self, query: str, *, limit: int = 8):
        slug = str(abs(hash(query)))[:6]
        return [
            SearchResult(
                title="Official source",
                url=f"https://example.gov/{slug}",
                snippet=f"Official evidence about {query} with market data and current facts.",
                provider="fake",
                query=query,
                published_at="2026-08-20T00:00:00+00:00",
                rank=1,
            ),
            SearchResult(
                title="Independent source",
                url=f"https://independent.example/{slug}",
                snippet=f"Independent corroboration about {query} and related analysis.",
                provider="fake",
                query=query,
                published_at="2026-08-18T00:00:00+00:00",
                rank=2,
            ),
        ][:limit]


class FakeFetcher:
    async def fetch(self, item: SearchResult) -> FetchResult:
        return FetchResult(
            url=item.url,
            title=item.title,
            text=(item.snippet + " ") * 20,
            content_type="text/html",
            status_code=200,
        )


class FakeLLM:
    available = True

    async def plan(self, task: str, *, mode: str, max_queries: int, max_sources: int):
        queries = [task] if mode == "quick" else [task, task + " official", task + " independent"]
        return ResearchPlan(task=task, mode=mode, queries=queries[:max_queries], max_sources=max_sources)

    async def quick_answer(self, task, evidence):
        return "这是一个简短联网回答 [1]，并由第二来源交叉支持 [2]。"

    async def analyze(self, task, evidence):
        return ResearchAnalysis(
            summary="多来源信息形成一致方向，但仍应关注数据发布时间。",
            findings=[
                ResearchFinding("核心结论由官方与独立来源共同支持。", [1, 2], 0.9),
                ResearchFinding("部分细节仍需要持续更新。", [2], 0.72),
            ],
            conflicts=[ResearchConflict("未发现足以推翻核心结论的直接冲突。", [1, 2])],
            gaps=["后续最新披露可能改变短期判断。"],
        )

    async def report(self, task, analysis, evidence):
        return await DeepSeekResearchLLM().report(task, analysis, evidence)


async def test_search_router_failover() -> None:
    router = SearchRouter(backends=[EmptyBackend(), WorkingBackend()])
    router.preferred = "auto"
    results = await router.search("test", limit=3)
    assert len(results) == 1
    assert router.last_backend == "working"


async def test_provider_runtime_background_execution() -> None:
    runtime = ProviderRuntime()
    runtime.register(ResearchAdapter(SlowEngine()))
    record = await runtime.start(ProviderRunRequest(provider="research", task="slow research"))
    assert record.task_handle is not None
    assert not record.task_handle.done(), "ProviderRuntime.start must not wait for deep research"
    await asyncio.sleep(0.01)
    assert record.status == "running"
    await record.task_handle
    assert record.status == "done"
    assert "研究报告" in record.result


async def test_engine_quick_and_deep() -> None:
    engine = ResearchEngine(
        search_router=FakeSearchRouter(),
        fetcher=FakeFetcher(),
        ranker=SourceRanker(),
        llm=FakeLLM(),
    )
    quick = await engine.run("查询示例公司当前公开信息", mode="quick")
    assert quick.mode == "quick"
    assert "# 研究报告" not in quick.text
    assert "来源：" in quick.text
    assert "[1]" in quick.text
    assert quick.evidence and quick.evidence[0].score > 0

    deep = await engine.run("深入分析示例行业趋势、风险和竞争格局", mode="deep")
    assert deep.mode == "deep"
    assert deep.text.startswith("# 研究报告：")
    assert "## 关键发现" in deep.text
    assert "[1][2]" in deep.text
    assert "## 来源质量" in deep.text
    assert len(deep.evidence) >= 2


async def test_adapter_lifecycle() -> None:
    engine = ResearchEngine(
        search_router=FakeSearchRouter(),
        fetcher=FakeFetcher(),
        ranker=SourceRanker(),
        llm=FakeLLM(),
    )
    adapter = ResearchAdapter(engine)
    events = []

    async def emit(event):
        events.append(event)

    result = await adapter.run(
        ProviderRunRequest(provider="research", task="深入研究示例行业并形成报告"),
        "research_test",
        emit,
    )
    assert result.status == "done"
    assert result.metadata["research_output_mode"] == "deep"
    assert result.activity_evidence is not None
    assert result.activity_evidence.execution_items >= 1
    assert any(event.type == "artifact.created" for event in events)
    assert any(event.type == "semantic.progress" for event in events)


def test_parsers_and_ranker() -> None:
    html = """
    <html><head><title>Example</title><script>bad()</script></head>
    <body><nav>menu</nav><main><h1>Heading</h1><p>This is a sufficiently long paragraph for extraction and testing purposes.</p></main></body></html>
    """
    title, text = extract_html_text(html)
    assert title == "Example"
    assert "sufficiently long paragraph" in text
    assert "bad()" not in text

    ddg = """
    <div class='result'><a class='result__a' href='/l/?uddg=https%3A%2F%2Fexample.com%2Fa'>Result A</a><a class='result__snippet'>Snippet A text</a></div>
    <div class='result'><a class='result__a' href='https://example.org/b'>Result B</a><a class='result__snippet'>Snippet B text</a></div>
    """
    results = parse_duckduckgo_html(ddg, "query", limit=5)
    assert len(results) == 2
    assert results[0].provider == "duckduckgo"
    assert results[0].rank == 1
    assert results[0].url == "https://example.com/a"


def test_provider_selection() -> None:
    manifests = (RESEARCH_MANIFEST, BROWSER_MANIFEST, OPENCLAW_MANIFEST)
    req = compile_delegate_requirements(
        DelegateRequirementFacts(
            requested_provider="browser",
            source_has_browser_address=False,
        )
    )
    assert req.task_kind == "research"
    selection = select_provider(req, manifests, default_provider="openclaw")
    assert selection.provider_id == "research", selection

    general = compile_delegate_requirements(DelegateRequirementFacts())
    assert general.task_kind == "general"
    # Research no longer declares general work and therefore cannot steal
    # arbitrary non-research tasks from other agent providers.
    selection2 = select_provider(general, manifests, default_provider="openclaw")
    assert selection2.provider_id == "openclaw", selection2

    continuation = compile_delegate_requirements(
        DelegateRequirementFacts(continuation_provider="research")
    )
    assert continuation.task_kind == "research"
    assert select_provider(continuation, manifests, default_provider="openclaw").provider_id == "research"

    addon = render_provider_routing_addon(
        language="en", provider_ids=("browser", "research", "openclaw")
    )
    assert 'Research (provider="research")' in addon
    assert "independently of OpenClaw" in addon


def test_citation_sanitizer() -> None:
    clean = DeepSeekResearchLLM.sanitize_citations("ok [1] bad [9] keep [2]", 2)
    assert clean == "ok [1] bad  keep [2]"


async def main() -> None:
    test_parsers_and_ranker()
    test_provider_selection()
    test_citation_sanitizer()
    await test_search_router_failover()
    await test_provider_runtime_background_execution()
    await test_engine_quick_and_deep()
    await test_adapter_lifecycle()
    print("PHASE3_RESEARCH_SMOKE_OK")


if __name__ == "__main__":
    asyncio.run(main())
