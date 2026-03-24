import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from core.loop import AgentLoop4
from mcp_servers.multi_mcp import MultiMCP


CASES_PATH = BASE_DIR / "test_cases.json"
RESULTS_DIR = BASE_DIR / "results"


def _extract_answer_text(summary: dict) -> str:
    final_outputs = summary.get("final_outputs", {})
    if isinstance(final_outputs, dict):
        text_values = [v for v in final_outputs.values() if isinstance(v, str) and v.strip()]
        if text_values:
            return max(text_values, key=len)
        return json.dumps(final_outputs, ensure_ascii=False)
    return str(final_outputs)


def _score_result(answer_text: str, expect_keywords: list[str]) -> dict:
    answer_lower = (answer_text or "").lower()
    found = [kw for kw in expect_keywords if kw.lower() in answer_lower]
    return {
        "keyword_hits": found,
        "keyword_hit_rate": round(len(found) / max(len(expect_keywords), 1), 3),
        "answer_length": len(answer_text or ""),
    }


async def run_benchmark():
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)
    case_limit = int((os.getenv("BENCH_CASE_LIMIT") or "0").strip())
    case_timeout_sec = int((os.getenv("BENCH_CASE_TIMEOUT_SEC") or "240").strip())
    if case_limit > 0:
        cases = cases[:case_limit]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    multi_mcp = MultiMCP()
    await multi_mcp.start()
    try:
        loop = AgentLoop4(multi_mcp=multi_mcp)
        for case in cases:
            started = time.time()
            case_id = case.get("id")
            query = case.get("query", "")
            expect_keywords = case.get("expect_keywords", [])
            try:
                context = await asyncio.wait_for(
                    loop.run(
                        query=query,
                        file_manifest=[],
                        globals_schema={},
                        uploaded_files=[],
                    ),
                    timeout=case_timeout_sec,
                )
                summary = context.get_execution_summary()
                answer_text = _extract_answer_text(summary)
                quality = _score_result(answer_text, expect_keywords)
                failed_steps = int(summary.get("failed_steps", 0))
                status = "passed" if failed_steps == 0 and quality["answer_length"] > 250 else "failed"
                results.append(
                    {
                        "id": case_id,
                        "status": status,
                        "duration_sec": round(time.time() - started, 2),
                        "failed_steps": failed_steps,
                        "completed_steps": int(summary.get("completed_steps", 0)),
                        "total_cost": summary.get("total_cost", 0.0),
                        "quality": quality,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "id": case_id,
                        "status": "error",
                        "duration_sec": round(time.time() - started, 2),
                        "error": str(exc),
                    }
                )
    finally:
        await multi_mcp.stop()

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_cases": len(results),
        "passed": sum(1 for r in results if r.get("status") == "passed"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "results": results,
        "config": {
            "case_limit": case_limit,
            "case_timeout_sec": case_timeout_sec,
        },
    }

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"research_smoke_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(str(out_path))


if __name__ == "__main__":
    asyncio.run(run_benchmark())
