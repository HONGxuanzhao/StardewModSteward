# 第三方组件与来源

本仓库中的 `app.py`、`core.py` 和测试实现本工具的逻辑，不包含游戏、SMAPI 或第三方 MOD 的源码/素材。运行与打包依赖以下项目：

| 组件 | 本次使用版本 | 来源 |
| --- | --- | --- |
| Python | 3.13.9 | https://www.python.org/ |
| Qt / PySide6 / Shiboken | 6.9.2 | https://www.qt.io/qt-for-python |
| requests | 2.32.5 | https://requests.readthedocs.io/ |
| PyInstaller | 6.22.3 | https://pyinstaller.org/ |

Qt/PySide6 的相应动态链接组件未修改，适用 LGPL 等对应组件许可。Windows 发行包附带第三方许可目录；兼容的动态链接库保留为独立文件。构建脚本会从本机已安装依赖的许可文件以及可用的 Conda 许可缓存收集文本。重新分发自己构建的二进制包时，应核对实际包含组件及其许可文件。

- Qt 6.9.2 源码：https://download.qt.io/archive/qt/6.9/6.9.2/submodules/
- PySide6 6.9.2 源码：https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.9.2
- Python 3.13.9 源码：https://www.python.org/downloads/release/python-3139/
- PyInstaller 许可：https://pyinstaller.org/en/stable/license.html

ZIP 支持来自 Python 标准库。7Z/RAR 功能调用用户本机的官方 7-Zip，发行包不捆绑 7-Zip。

在线功能使用 [SMAPI.io API](https://github.com/Pathoschild/SMAPI/blob/develop/docs/technical/web.md) 与 [Nexus Mods API](https://github.com/Nexus-Mods/node-nexus-api)。本工具与这些服务及游戏官方没有隶属关系。
