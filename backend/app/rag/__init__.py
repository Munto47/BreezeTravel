"""
Advanced RAG 模块

子模块：
- embedder  : 文本向量化（OpenAI / DeepSeek 兼容接口）
- hyde      : HyDE 查询扩展（Hypothetical Document Embeddings）
- retriever : 混合检索（Dense pgvector + Sparse BM25）+ RRF 融合
- reranker  : Cross-Encoder 重排序（BAAI/bge-reranker-v2-m3）
"""
