# OpenTHS

OpenTHS 为终端和 AI 助手提供 20 个同花顺只读 JSON 命令。独立仓库包含协议实现、技能入口和运行资源。运行要求 Python 3.13 或更高版本，仅使用标准库。

## 使用

从 GitHub 克隆或下载完整仓库，在技能目录运行以下离线命令。

```sh
python scripts/query.py --help
python scripts/query.py capabilities
python scripts/query.py boards
python scripts/query.py sector-capabilities
python scripts/query.py limit-capabilities
```

实时查询需显式传入 `--live`。TCP 查询另需端点、本地 App 配置和 app-info。成交额比较使用内置 2026 年日历。从其他目录运行时使用脚本和配置的绝对路径。详见[命令参考](references/commands.md)和[宿主接入](references/integration.md)。

## 验证

```sh
python -I -S -m unittest discover -s tests -v
```

安装 `requirements-dev.txt` 后可运行 `python -m ruff check .` 和 `python -m ruff format --check .`。协议来源和历史实网记录见[验证范围](references/scope.md)。许可和第三方材料说明分别见 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
