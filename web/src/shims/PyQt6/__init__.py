"""浏览器里用的 PyQt6 空壳。

分析引擎里只有 pa_agent.util.event_bus 等少数模块为了桌面端依赖 PyQt6；
在 Web 端这些模块只被 import、不会被实例化，所以提供最小可用替身即可。
"""
