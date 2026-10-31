"""tests.contract 包：HTTP/文档契约测试。

本目录用例用 FastAPI ``TestClient`` 或静态口径断言校验对外契约（字段、状态码、
错误码、文档映射），不依赖真实 PostgreSQL；需要真实库的链路在
``tests/integration``。运行：``pytest tests/contract -q``。
"""
