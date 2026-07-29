"""NewsRadarDaemon 结构级回归测试。

`run()` 是完整 daemon 生命周期(DB/信号/Web/worker/timer),无法纯单元测试。
此处用源码级断言守护关键不变量。
"""
import inspect


def test_run_inits_base_prompt_before_models_block():
    """回归:base_prompt 必须在 `if self.config.get("models")` 块前初始化。

    `create_app(..., base_prompt=base_prompt)` 在方法体级(if 块外)引用,
    若 base_prompt 仅在 if models 块内绑定,无 models 路径会 NameError。
    """
    from main import NewsRadarDaemon
    src = inspect.getsource(NewsRadarDaemon.run).splitlines()
    # 定位第一个 `if self.config.get("models"):` 块
    models_if_idx = next(
        i for i, ln in enumerate(src)
        if "self.config.get(\"models\")" in ln and ln.lstrip().startswith("if")
    )
    before = [ln.strip() for ln in src[:models_if_idx]]
    assert "base_prompt = \"\"" in before, (
        "base_prompt 必须在 if models 块前初始化,否则无 models 路径 NameError"
    )
