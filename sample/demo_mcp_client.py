#!/usr/bin/env python3
"""
llm-helper MCP Server Demo Client

MCPサーバーとして起動したllm-helperに接続し、
各種ツールを呼び出すデモプログラムです。

Usage:
    # 1. 別ターミナルでMCPサーバーを起動
    cd /home/kazuki/public_html/llm-helper
    python code_intel_server.py --transport stdio

    # 2. このデモを実行
    python sample/demo_mcp_client.py
"""

import asyncio
import json
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


async def demo_direct_tool_calls():
    """ツールモジュールを直接呼び出すデモ"""
    print("=" * 60)
    print("Demo: Direct Tool Calls (without MCP)")
    print("=" * 60)

    # ContextProvider のデモ
    print("\n--- ContextProvider Demo ---")
    from tools.context_provider import ContextProvider

    provider = ContextProvider(str(project_root))
    context = provider.load_context()

    if context is None:
        print("  context.yml not found. Run 'start_session' MCP tool first.")
    else:
        print(f"Design Docs: {len(context.design_docs)}")
        for doc in context.design_docs:
            print(f"  - {doc.path}: {doc.summary[:50]}..." if doc.summary else f"  - {doc.path}: (no summary)")

        print(f"\nProject Rules: {len(context.project_rules)}")
        for rule in context.project_rules:
            print(f"  - {rule.path}")

    # ImpactAnalyzer のデモ
    print("\n--- ImpactAnalyzer Demo ---")
    from tools.impact_analyzer import ImpactAnalyzer

    analyzer = ImpactAnalyzer(str(project_root))
    result = await analyzer.analyze(
        target_files=["code_intel_server.py"],
        change_description="MCPサーバーのデモ"
    )

    print(f"Mode: {result.mode}")
    print(f"Static References: {len(result.static_references)}")
    print(f"Naming Convention Matches: {len(result.naming_convention_matches)}")


async def demo_session_workflow():
    """セッションワークフローのデモ（擬似的）"""
    print("\n" + "=" * 60)
    print("Demo: Session Workflow (Simulated)")
    print("=" * 60)

    # 実際のMCPクライアントではなく、ツールの内部APIを直接呼び出す
    # MCPプロトコル経由での呼び出しはClaude Code等のMCPクライアントを使用

    workflow_steps = [
        ("1. start_session", "セッション開始、intent=IMPLEMENT"),
        ("2. set_query_frame", "QueryFrameスロット設定"),
        ("3. submit_understanding", "探索結果提出 → SEMANTICへ"),
        ("4. semantic_search", "セマンティック検索実行"),
        ("5. submit_semantic", "仮説提出 → VERIFICATIONへ"),
        ("6. submit_verification", "検証結果提出 → IMPACT_ANALYSISへ"),
        ("7. analyze_impact", "影響分析実行"),
        ("8. submit_impact_analysis", "影響分析提出 → READYへ"),
        ("9. Edit/Write", "実装（READY状態でのみ許可）"),
        ("10. record_outcome", "結果記録"),
    ]

    print("\nPhase Gate Workflow:")
    for step, desc in workflow_steps:
        print(f"  {step}: {desc}")

    print("\nNote: 実際のMCP呼び出しはClaude Code等のMCPクライアントを使用してください")


async def demo_chromadb_search():
    """ChromaDBセマンティック検索のデモ"""
    print("\n" + "=" * 60)
    print("Demo: ChromaDB Semantic Search")
    print("=" * 60)

    try:
        import chromadb
        from chromadb.config import Settings

        db_path = project_root / ".code-intel" / "chromadb"
        if not db_path.exists():
            print(f"ChromaDB not found at {db_path}")
            print("Run 'sync_index' MCP tool first to create the index")
            return

        client = chromadb.PersistentClient(
            path=str(db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        # コレクション一覧
        collections = client.list_collections()
        print(f"\nCollections: {[c.name for c in collections]}")

        # forestコレクションでの検索デモ
        forest_name = f"forest_{project_root.name}"
        try:
            forest = client.get_collection(forest_name)
            print(f"\nForest collection '{forest_name}':")
            print(f"  Count: {forest.count()}")

            # サンプル検索
            if forest.count() > 0:
                results = forest.query(
                    query_texts=["session management"],
                    n_results=3
                )
                print("\n  Sample search for 'session management':")
                for i, (id_, distance) in enumerate(zip(results['ids'][0], results['distances'][0])):
                    print(f"    {i+1}. {id_} (distance: {distance:.4f})")
        except Exception as e:
            print(f"  Forest collection not found: {e}")

    except ImportError:
        print("ChromaDB not installed. Run: pip install chromadb")


async def main():
    """メインエントリーポイント"""
    print("llm-helper MCP Server Demo")
    print("Project root:", project_root)

    await demo_direct_tool_calls()
    await demo_session_workflow()
    await demo_chromadb_search()

    print("\n" + "=" * 60)
    print("Demo Complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
