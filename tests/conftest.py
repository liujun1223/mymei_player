"""pytest 全局配置：Linux 下加载 HA 测试插件。."""

import sys

# HA 依赖 Linux 专有模块（fcntl），Windows 上无法加载该插件
if sys.platform != "win32":
    pytest_plugins = "pytest_homeassistant_custom_component"
