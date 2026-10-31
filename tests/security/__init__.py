"""tests.security 包：安全门禁与红队测试。

本目录用例覆盖提示注入语料、MFA 凭据、沙箱逃逸、熔断护栏、越权提权、幂等去重、
未知结果不重试、审计不可变与补偿护栏，直接调用 ``Struct_of_Agent`` 的安全模块，
不需要起服务。运行：``pytest tests/security -q``。
"""
