from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.config import PROJECT_ROOT, Settings
from app.rag.chunking import chunk_document
from app.rag.cleaning import clean_page_text, extract_document, table_to_markdown
from app.rag.embeddings import HashingEmbedding, lexical_tokens
from app.rag.indexer import build_index
from app.rag.retriever import HybridRetriever


PDF_PATH = PROJECT_ROOT / "步行指数知识库.pdf"


def settings_for(root: Path) -> Settings:
    return Settings(
        database_path=root / "index" / "rag.sqlite3",
        processed_path=root / "processed" / "chunks.jsonl",
        source_glob="*.pdf",
        embedding_provider="hash",
        embedding_dimension=384,
        embedding_base_url="http://localhost:7997/v1",
        embedding_model="BAAI/bge-m3",
        embedding_api_key="",
        dense_weight=0.55,
        lexical_weight=0.35,
        number_weight=0.10,
        default_top_k=5,
        spring_boot_base_url="http://localhost:8080",
    )


class CleaningTests(unittest.TestCase):
    def test_formula_layout_is_repaired(self) -> None:
        raw = """步行指数知识库 · 大连市社区生活圈案例
第 5 页
3.4 步行指数公式与评分
n
S = ∑ W × (1 - α) × (1 - β)
a b=1 b
S\x00 小区a的步行指数
W\x00 b类基础设施的权重值
"""
        cleaned, warnings = clean_page_text(raw, 5)
        self.assertIn("S_a = ∑_{b=1}^{n} W_b", cleaned)
        self.assertIn("S_a 小区a的步行指数", cleaned)
        self.assertIn("W_b b类基础设施的权重值", cleaned)
        self.assertIn("formula_layout_repaired_from_visual_source", warnings)

    def test_table_is_serialized_as_markdown(self) -> None:
        markdown, rows, columns = table_to_markdown([["设施", "覆盖率"], ["购物服务", "98.86%"]])
        self.assertEqual(rows, 2)
        self.assertEqual(columns, 2)
        self.assertIn("| 购物服务 | 98.86% |", markdown)

    def test_actual_pdf_has_tables_and_warning_chunks(self) -> None:
        document = extract_document(PDF_PATH)
        chunks = chunk_document(document)
        self.assertEqual(document.page_count, 12)
        self.assertGreaterEqual(sum(len(page.tables) for page in document.pages), 10)
        self.assertTrue(any(chunk.content_type == "table" for chunk in chunks))
        self.assertTrue(any(chunk.warnings for chunk in chunks))
        self.assertTrue(any("S_a = ∑_{b=1}^{n} W_b" in chunk.content for chunk in chunks))


class EmbeddingTests(unittest.TestCase):
    def test_hash_embedding_is_stable_and_normalized(self) -> None:
        provider = HashingEmbedding(128)
        first = provider.embed(["购物服务平均覆盖率"])
        second = provider.embed(["购物服务平均覆盖率"])
        np.testing.assert_array_equal(first, second)
        self.assertAlmostEqual(float(np.linalg.norm(first[0])), 1.0, places=6)

    def test_chinese_lexical_tokens_include_bigrams_and_numbers(self) -> None:
        tokens = lexical_tokens("购物服务覆盖率为98.86%")
        self.assertIn("购物", tokens)
        self.assertIn("覆盖", tokens)
        self.assertIn("98.86%", tokens)


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.settings = settings_for(Path(cls.tempdir.name))
        cls.manifest = build_index(cls.settings, [PDF_PATH])
        cls.retriever = HybridRetriever(cls.settings)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def assert_top_results_contain(self, query: str, expected: str, top_k: int = 5) -> None:
        results = self.retriever.search(query, top_k=top_k)
        combined = "\n".join(result.chunk.content for result in results)
        self.assertIn(expected, combined, msg=f"Expected {expected!r} in results for {query!r}")
        self.assertTrue(all(result.citation for result in results))

    def test_manifest_records_structured_content(self) -> None:
        self.assertEqual(self.manifest["documentCount"], 1)
        self.assertGreaterEqual(self.manifest["contentTypes"]["table"], 10)
        self.assertGreater(self.manifest["warningChunkCount"], 0)

    def test_retrieves_intersection_density(self) -> None:
        self.assert_top_results_contain("生活圈平均道路交叉口密度是多少？", "73个/km²")

    def test_retrieves_shopping_coverage(self) -> None:
        self.assert_top_results_contain("购物服务的平均覆盖率是多少？", "98.86%")

    def test_retrieves_walkability_formula(self) -> None:
        results = self.retriever.search("步行指数如何计算？", top_k=3)
        self.assertIn("S_a = ∑_{b=1}^{n} W_b", results[0].chunk.content)
        self.assertEqual(results[0].chunk.content_type, "formula")

    def test_section_page_range_does_not_include_next_heading_page(self) -> None:
        results = self.retriever.search("步行指数平均值83分", top_k=5)
        matching = next(result for result in results if "平均值为83" in result.chunk.content)
        self.assertEqual(matching.chunk.page_end, 9)

    def test_retrieves_source_uncertainty(self) -> None:
        results = self.retriever.search("2229个小区高于平均值的占比是多少？", top_k=5)
        self.assertTrue(any(result.chunk.warnings for result in results))
        self.assertTrue(any("68.05%" in result.chunk.content for result in results))

    def test_filters_content_type(self) -> None:
        results = self.retriever.search("覆盖率", top_k=5, content_types=["table"])
        self.assertTrue(results)
        self.assertTrue(all(result.chunk.content_type == "table" for result in results))


if __name__ == "__main__":
    unittest.main()
